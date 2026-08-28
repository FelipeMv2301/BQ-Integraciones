"""Tests de app.tasks.locks::pipeline_lock — con Redis fake, sin red."""

from app.tasks.locks import pipeline_lock


class _FakeRedisLock:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


def test_lock_se_adquiere_si_esta_libre(monkeypatch):
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: _FakeRedisLock())

    with pipeline_lock("poll_x") as acquired:
        assert acquired is True


def test_segunda_corrida_con_lock_ya_tomado_no_adquiere(monkeypatch):
    fake = _FakeRedisLock()
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake)

    with pipeline_lock("poll_x") as primero:
        assert primero is True
        with pipeline_lock("poll_x") as segundo:
            assert segundo is False


def test_lock_se_libera_al_salir_normalmente(monkeypatch):
    fake = _FakeRedisLock()
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake)

    with pipeline_lock("poll_y") as primero:
        assert primero is True
    with pipeline_lock("poll_y") as segundo:
        assert segundo is True


def test_no_libera_lock_ajeno_si_no_se_adquirio(monkeypatch):
    """Si el lock ya estaba tomado por otro proceso, salir del context NO debe borrarlo."""
    fake = _FakeRedisLock()
    fake.store["pipeline:lock:poll_w"] = "1"
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake)

    with pipeline_lock("poll_w") as acquired:
        assert acquired is False
    assert fake.store.get("pipeline:lock:poll_w") == "1"


def test_redis_caido_corre_sin_lock_fail_open(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("Redis inalcanzable")
    monkeypatch.setattr("redis.Redis.from_url", _raise)

    with pipeline_lock("poll_z") as acquired:
        assert acquired is True
