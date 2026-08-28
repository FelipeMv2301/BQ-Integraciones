"""Tests de app.services.sap.exchange_rates — con SAP/mindicador.cl/Redis mockeados."""

from datetime import date

import pytest

from app.services.sap import exchange_rates


class _FakeRespuesta:
    def __init__(self, ok, status_code=200, data=None):
        self.ok = ok
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeMindicadorResponse:
    def __init__(self, serie):
        self._serie = serie

    def raise_for_status(self):
        pass

    def json(self):
        return {"serie": self._serie}


def _sin_cache(monkeypatch):
    monkeypatch.setattr(exchange_rates, "_leer_cache", lambda fecha: False)
    monkeypatch.setattr(exchange_rates, "_guardar_cache", lambda fecha: None)


def test_existe_tasa_true_si_sap_responde_ok(monkeypatch):
    monkeypatch.setattr(exchange_rates.client, "solicitar", lambda *a, **k: _FakeRespuesta(ok=True))
    assert exchange_rates._existe_tasa(date(2026, 8, 18)) is True


def test_existe_tasa_false_si_error_4006(monkeypatch):
    respuesta = _FakeRespuesta(ok=False, status_code=400, data={"error": {"code": -4006}})
    monkeypatch.setattr(exchange_rates.client, "solicitar", lambda *a, **k: respuesta)
    assert exchange_rates._existe_tasa(date(2026, 8, 6)) is False


def test_existe_tasa_propaga_otros_errores(monkeypatch):
    respuesta = _FakeRespuesta(ok=False, status_code=500, data={"error": {"code": -999}})
    monkeypatch.setattr(exchange_rates.client, "solicitar", lambda *a, **k: respuesta)
    with pytest.raises(RuntimeError):
        exchange_rates._existe_tasa(date(2026, 8, 6))


def test_asegurar_tasa_cambio_no_hace_nada_si_ya_existe(monkeypatch):
    _sin_cache(monkeypatch)
    monkeypatch.setattr(exchange_rates, "_existe_tasa", lambda fecha: True)
    llamado_set = []
    monkeypatch.setattr(exchange_rates, "_cargar_tasa", lambda fecha, valor: llamado_set.append(valor))

    exchange_rates.asegurar_tasa_cambio(date(2026, 8, 18))

    assert llamado_set == []


def test_asegurar_tasa_cambio_trae_y_carga_si_falta(monkeypatch):
    _sin_cache(monkeypatch)
    monkeypatch.setattr(exchange_rates, "_existe_tasa", lambda fecha: False)
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers, timeout: _FakeMindicadorResponse([{"valor": 911.58}]),
    )
    llamados_carga = []
    monkeypatch.setattr(
        exchange_rates.client, "solicitar",
        lambda metodo, endpoint, json_body: llamados_carga.append((endpoint, json_body)) or _FakeRespuesta(ok=True),
    )

    exchange_rates.asegurar_tasa_cambio(date(2026, 8, 6))

    assert llamados_carga == [
        ("SBOBobService_SetCurrencyRate", {"RateDate": "2026-08-06", "Currency": "USD", "Rate": "911.58"}),
    ]


def test_asegurar_tasa_cambio_falla_si_mindicador_sin_dato(monkeypatch):
    _sin_cache(monkeypatch)
    monkeypatch.setattr(exchange_rates, "_existe_tasa", lambda fecha: False)
    monkeypatch.setattr("requests.get", lambda url, headers, timeout: _FakeMindicadorResponse([]))

    with pytest.raises(exchange_rates.TasaCambioError):
        exchange_rates.asegurar_tasa_cambio(date(2026, 8, 6))


def test_cache_evita_segunda_consulta_a_sap_el_mismo_dia(monkeypatch):
    store = {}
    monkeypatch.setattr(exchange_rates, "_leer_cache", lambda fecha: exchange_rates._cache_key(fecha) in store)
    monkeypatch.setattr(exchange_rates, "_guardar_cache", lambda fecha: store.__setitem__(exchange_rates._cache_key(fecha), True))

    llamadas = []
    def _existe(fecha):
        llamadas.append(fecha)
        return True
    monkeypatch.setattr(exchange_rates, "_existe_tasa", _existe)

    exchange_rates.asegurar_tasa_cambio(date(2026, 8, 18))
    exchange_rates.asegurar_tasa_cambio(date(2026, 8, 18))

    assert len(llamadas) == 1
