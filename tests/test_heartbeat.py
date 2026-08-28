"""Tests de app.tasks.heartbeat::heartbeat — con requests mockeado, sin red."""

import pytest

from app.core.config import settings
from app.tasks.heartbeat import heartbeat


def test_healthchecks_checks_parsea_mapeo_slug_uuid(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECKS_CHECKS", "a:111, b:222 ,")
    assert settings.healthchecks_checks == {"a": "111", "b": "222"}


def test_no_op_si_el_slug_no_tiene_check_configurado(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECKS_CHECKS", "")
    llamadas = []
    monkeypatch.setattr("requests.get", lambda *a, **k: llamadas.append(a))

    with heartbeat("algo"):
        pass

    assert llamadas == []


def test_ping_start_y_exito_si_todo_sale_bien(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECKS_CHECKS", "mi-tarea:uuid-abc")
    monkeypatch.setattr(settings, "HEALTHCHECKS_BASE_URL", "https://hc-ping.com")
    urls = []
    monkeypatch.setattr("requests.get", lambda url, timeout: urls.append(url))

    with heartbeat("mi-tarea"):
        pass

    assert urls == [
        "https://hc-ping.com/uuid-abc/start",
        "https://hc-ping.com/uuid-abc",
    ]


def test_ping_start_y_fail_si_la_tarea_lanza(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECKS_CHECKS", "mi-tarea:uuid-abc")
    monkeypatch.setattr(settings, "HEALTHCHECKS_BASE_URL", "https://hc-ping.com")
    urls = []
    monkeypatch.setattr("requests.get", lambda url, timeout: urls.append(url))

    with pytest.raises(ValueError):
        with heartbeat("mi-tarea"):
            raise ValueError("boom")

    assert urls == [
        "https://hc-ping.com/uuid-abc/start",
        "https://hc-ping.com/uuid-abc/fail",
    ]


def test_ping_fallido_no_rompe_el_pipeline(monkeypatch):
    """Best-effort: si Healthchecks no responde, la tarea sigue corriendo igual."""
    monkeypatch.setattr(settings, "HEALTHCHECKS_CHECKS", "mi-tarea:uuid-abc")

    def _raise(*a, **k):
        raise ConnectionError("Healthchecks inalcanzable")
    monkeypatch.setattr("requests.get", _raise)

    ejecuto = False
    with heartbeat("mi-tarea"):
        ejecuto = True
    assert ejecuto is True
