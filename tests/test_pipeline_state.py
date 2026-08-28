"""Tests de app.core.pipeline_state — con Redis fake, sin red."""

from app.core import pipeline_state


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def test_apagado_por_defecto_si_no_hay_nada_en_redis(monkeypatch):
    monkeypatch.setattr(pipeline_state, "_get_redis", lambda: _FakeRedis())
    assert pipeline_state.is_enabled() is False


def test_enable_prende_y_disable_apaga(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(pipeline_state, "_get_redis", lambda: fake)

    pipeline_state.enable()
    assert pipeline_state.is_enabled() is True

    pipeline_state.disable()
    assert pipeline_state.is_enabled() is False


def test_redis_caido_falla_cerrado(monkeypatch):
    """A diferencia de pipeline_lock (fail-open), este flag falla APAGADO."""
    def _raise():
        raise ConnectionError("Redis inalcanzable")
    monkeypatch.setattr(pipeline_state, "_get_redis", _raise)

    assert pipeline_state.is_enabled() is False
