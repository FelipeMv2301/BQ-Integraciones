"""Tests de app.core.sentry::init_sentry — sin red real a Sentry."""

import sys
import types

import pytest

import app.core.sentry as sentry_module
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_estado_global():
    sentry_module._initialized = False
    yield
    sentry_module._initialized = False


def _fake_sentry_sdk(monkeypatch):
    llamadas = {"init": [], "tags": []}
    modulo = types.SimpleNamespace(
        init=lambda **kw: llamadas["init"].append(kw),
        set_tag=lambda k, v: llamadas["tags"].append((k, v)),
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", modulo)
    return llamadas


def test_no_op_si_no_hay_dsn(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_DSN", "")
    llamadas = _fake_sentry_sdk(monkeypatch)

    sentry_module.init_sentry("web")

    assert llamadas["init"] == []


def test_inicializa_con_dsn_configurado(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://fake@sentry.example/1")
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    llamadas = _fake_sentry_sdk(monkeypatch)

    sentry_module.init_sentry("web")

    assert len(llamadas["init"]) == 1
    assert llamadas["init"][0]["dsn"] == "https://fake@sentry.example/1"
    assert llamadas["init"][0]["send_default_pii"] is False
    assert llamadas["tags"] == [("component", "web")]


def test_segunda_llamada_no_reinicializa(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://fake@sentry.example/1")
    llamadas = _fake_sentry_sdk(monkeypatch)

    sentry_module.init_sentry("web")
    sentry_module.init_sentry("worker")

    assert len(llamadas["init"]) == 1


def test_error_al_inicializar_no_rompe(monkeypatch):
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://fake@sentry.example/1")

    def _init_que_falla(**kw):
        raise RuntimeError("DSN inválido")

    modulo = types.SimpleNamespace(init=_init_que_falla, set_tag=lambda k, v: None)
    monkeypatch.setitem(sys.modules, "sentry_sdk", modulo)

    sentry_module.init_sentry("web")
