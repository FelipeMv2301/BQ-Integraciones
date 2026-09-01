"""Tests de app.main — middleware de API Key (BQI-64), con TestClient real
(necesario porque el middleware solo se dispara en el ciclo HTTP real, no
llamando a las funciones de ruta directo como el resto de los tests)."""

from fastapi.testclient import TestClient

from app import main


def test_health_no_requiere_api_key_aunque_este_configurada(monkeypatch):
    monkeypatch.setattr(main.settings, "API_KEY", "secreta123")
    cliente = TestClient(main.app)

    respuesta = cliente.get("/health")

    assert respuesta.status_code == 200


def test_otro_endpoint_sin_key_devuelve_401(monkeypatch):
    monkeypatch.setattr(main.settings, "API_KEY", "secreta123")
    cliente = TestClient(main.app)

    respuesta = cliente.get("/pipeline/status")

    assert respuesta.status_code == 401
    assert "X-API-Key" in respuesta.json()["detail"]


def test_key_incorrecta_devuelve_401(monkeypatch):
    monkeypatch.setattr(main.settings, "API_KEY", "secreta123")
    cliente = TestClient(main.app)

    respuesta = cliente.get("/pipeline/status", headers={"X-API-Key": "otra-cosa"})

    assert respuesta.status_code == 401


def test_key_correcta_deja_pasar(monkeypatch):
    monkeypatch.setattr(main.settings, "API_KEY", "secreta123")
    cliente = TestClient(main.app)

    respuesta = cliente.get("/pipeline/status", headers={"X-API-Key": "secreta123"})

    assert respuesta.status_code == 200


def test_sin_api_key_configurada_no_exige_nada(monkeypatch):
    """Desarrollo local sin API_KEY seteada — mismo criterio que Stock-Service."""
    monkeypatch.setattr(main.settings, "API_KEY", "")
    cliente = TestClient(main.app)

    respuesta = cliente.get("/pipeline/status")

    assert respuesta.status_code == 200
