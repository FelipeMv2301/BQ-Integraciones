"""Tests de app.core.api_log — con Redis fake, sin red."""

from app.core import api_log


class _FakePipe:
    def __init__(self, store):
        self.store = store
        self._queued = []

    def lpush(self, key, value):
        self._queued.append(("lpush", value))
        return self

    def ltrim(self, key, start, end):
        self._queued.append(("ltrim", start, end))
        return self

    def lrange(self, key, start, end):
        self._queued.append(("lrange", start, end))
        return self

    def execute(self):
        resultados = []
        for cmd in self._queued:
            data = self.store.data
            n = len(data)
            if cmd[0] == "lpush":
                data.insert(0, cmd[1])
                resultados.append(len(data))
            elif cmd[0] == "ltrim":
                _, start, end = cmd
                s = start if start >= 0 else max(n + start, 0)
                e = end if end >= 0 else n + end
                self.store.data = data[s:e + 1] if e >= s else []
                resultados.append(True)
            elif cmd[0] == "lrange":
                _, start, end = cmd
                s = start if start >= 0 else max(n + start, 0)
                e = end if end >= 0 else n + end
                resultados.append(data[s:e + 1] if e >= s else [])
        self._queued = []
        return resultados


class _FakeRedis:
    def __init__(self):
        self.data = []

    def pipeline(self):
        return _FakePipe(self)


def _sin_redis(monkeypatch):
    """Fuerza _get_redis() a devolver None, como si Redis no respondiera."""
    monkeypatch.setattr(api_log, "_redis", None)
    monkeypatch.setattr(api_log, "_redis_failed", True)


def _con_redis_fake(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(api_log, "_redis", fake)
    monkeypatch.setattr(api_log, "_redis_failed", False)
    return fake


# ── _redact_url / _truncate ─────────────────────────────────────────────

def test_redact_url_oculta_credenciales_de_query_params():
    url = "https://x.com/products?consumer_key=ck_123&consumer_secret=cs_456&per_page=10"
    resultado = api_log._redact_url(url)
    assert "ck_123" not in resultado
    assert "cs_456" not in resultado
    assert "per_page=10" in resultado


def test_truncate_corta_a_2000_caracteres():
    texto = "x" * 3000
    assert len(api_log._truncate(texto)) == api_log.BODY_MAX_CHARS


def test_truncate_none_devuelve_none():
    assert api_log._truncate(None) is None


# ── log_api_call / drain_api_logs ───────────────────────────────────────

def test_log_api_call_sin_redis_no_explota(monkeypatch):
    _sin_redis(monkeypatch)
    api_log.log_api_call(api_name="SAP", method="GET", url="https://x.com")  # no debe lanzar


def test_log_api_call_encola_y_drain_lo_recupera(monkeypatch):
    _con_redis_fake(monkeypatch)

    api_log.log_api_call(api_name="SAP", method="GET", url="https://x.com/a", status_code=200)
    api_log.log_api_call(api_name="WooCommerce", method="GET", url="https://x.com/b", status_code=404)

    resultado = api_log.drain_api_logs(max_items=10)

    assert [r["api_name"] for r in resultado] == ["SAP", "WooCommerce"]  # orden cronológico
    assert resultado[1]["status_code"] == 404


def test_drain_api_logs_borra_lo_que_extrae(monkeypatch):
    fake = _con_redis_fake(monkeypatch)
    api_log.log_api_call(api_name="SAP", method="GET", url="https://x.com")

    api_log.drain_api_logs(max_items=10)

    assert fake.data == []


def test_drain_api_logs_sin_redis_devuelve_vacio(monkeypatch):
    _sin_redis(monkeypatch)
    assert api_log.drain_api_logs() == []


# ── make_response_hook ──────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, method, url, body=None):
        self.method = method
        self.url = url
        self.body = body


class _FakeElapsed:
    def total_seconds(self):
        return 0.123


class _FakeResponse:
    def __init__(self, request, status_code, text=""):
        self.request = request
        self.status_code = status_code
        self.text = text
        self.elapsed = _FakeElapsed()

    @property
    def ok(self):
        return 200 <= self.status_code < 300


def test_hook_registra_llamada_exitosa_sin_guardar_response_body(monkeypatch):
    llamadas = []
    monkeypatch.setattr(api_log, "log_api_call", lambda **kw: llamadas.append(kw))
    hook = api_log.make_response_hook("SAP")
    request = _FakeRequest("GET", "https://sap.example/Invoices")

    resultado = hook(_FakeResponse(request, 200))

    assert resultado.status_code == 200
    assert llamadas[0]["api_name"] == "SAP"
    assert llamadas[0]["status_code"] == 200
    assert llamadas[0]["response_body"] is None


def test_hook_guarda_bodies_si_hay_error(monkeypatch):
    llamadas = []
    monkeypatch.setattr(api_log, "log_api_call", lambda **kw: llamadas.append(kw))
    hook = api_log.make_response_hook("SAP")
    request = _FakeRequest("POST", "https://sap.example/Invoices", body=b'{"x":1}')

    hook(_FakeResponse(request, 400, text="dato invalido"))

    assert llamadas[0]["request_body"] == '{"x":1}'
    assert llamadas[0]["response_body"] == "dato invalido"


def test_hook_no_guarda_body_en_paths_sensibles_de_sesion(monkeypatch):
    llamadas = []
    monkeypatch.setattr(api_log, "log_api_call", lambda **kw: llamadas.append(kw))
    hook = api_log.make_response_hook("TokenSAP")
    request = _FakeRequest("POST", "https://token-sap-bq.example/session", body=b'{"password":"secreto"}')

    hook(_FakeResponse(request, 200))

    assert llamadas[0]["request_body"] is None


def test_hook_nunca_lanza_si_la_respuesta_es_invalida():
    hook = api_log.make_response_hook("SAP")
    resultado = hook(None)
    assert resultado is None
