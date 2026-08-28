"""
Tests de app.services.sap.session — I5.

I5: si Token-SAP-BQ no responde (y no hay sesión cacheada vigente), el error
se propaga como TokenSAPBQError — NUNCA se degrada a un login directo
contra SAP. Se verifica en runtime (los tests de acá) y estáticamente
(test_ningun_archivo_hace_login_directo_a_sap, al final del archivo).
"""

import glob
import os

import pytest
import requests

from app.services.sap import session


@pytest.fixture(autouse=True)
def _reset_cache_en_memoria():
    """
    session._mem_cache es una variable de módulo (no una instancia nueva
    por test) — sin resetearla, lo que guarda un test se filtra al
    siguiente. Se limpia antes y después de cada test.
    """
    session._mem_cache = None
    yield
    session._mem_cache = None


class _RespuestaFallida:
    ok = False
    status_code = 502
    text = "Bad Gateway"


def test_token_sap_bq_inalcanzable_no_cae_a_login_directo(monkeypatch):
    monkeypatch.setattr(session, "_leer_cache", lambda: None)

    def _lanzar(*args, **kwargs):
        raise requests.exceptions.ConnectionError("Token-SAP-BQ inalcanzable")

    monkeypatch.setattr(session._http, "post", _lanzar)

    with pytest.raises(session.TokenSAPBQError):
        session.obtener_cookies()


def test_token_sap_bq_rechaza_credenciales(monkeypatch):
    monkeypatch.setattr(session, "_leer_cache", lambda: None)
    monkeypatch.setattr(session._http, "post", lambda *a, **k: _RespuestaFallida())

    with pytest.raises(session.TokenSAPBQError):
        session.obtener_cookies()


def test_sesion_cacheada_evita_llamar_a_token_sap_bq(monkeypatch):
    """Con caché vigente, obtener_cookies() no debe hacer ningún request."""
    monkeypatch.setattr(
        session, "_leer_cache",
        lambda: {"b1session": "sesion-cacheada", "routeid": "node1"},
    )

    llamado = {"post": False}

    def _fallar_si_se_llama(*a, **k):
        llamado["post"] = True
        raise AssertionError("no debería llamar a Token-SAP-BQ con caché vigente")

    monkeypatch.setattr(session._http, "post", _fallar_si_se_llama)

    cookies = session.obtener_cookies()

    assert cookies == {"B1SESSION": "sesion-cacheada", "ROUTEID": "node1"}
    assert llamado["post"] is False


def test_invalidar_limpia_cache_en_memoria(monkeypatch):
    session._mem_cache = {"b1session": "vieja", "routeid": "node1"}
    monkeypatch.setattr(session._redis, "delete", lambda *a, **k: None)
    monkeypatch.setattr(session._http, "post", lambda *a, **k: None)

    session.invalidar("vieja")

    assert session._mem_cache is None


def test_ningun_archivo_hace_login_directo_a_sap():
    """
    Verificación estática (I5): ningún archivo del proyecto debe contener
    una llamada real a POST .../Login. Se excluyen docstrings/comentarios
    que MENCIONAN /Login para explicar qué NO hacer.
    """
    app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
    infractores = []

    for path in glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as f:
            for numero_linea, linea in enumerate(f, start=1):
                limpia = linea.strip()
                parece_login = (".post(" in limpia and "/Login" in limpia) or (
                    "requests.post" in limpia and "Login" in limpia
                )
                if parece_login and not limpia.startswith("#"):
                    infractores.append(f"{path}:{numero_linea}: {limpia}")

    assert infractores == [], f"Posible llamada directa a SAP /Login: {infractores}"
