"""Tests de app.services.stockservice.client — sin red ni Redis real."""

from app.services.stockservice import client


class _Respuesta:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_obtener_producto_usa_cache_si_esta_vigente(monkeypatch):
    monkeypatch.setattr(client, "_leer_cache", lambda sku: {"sku": sku, "sap": {}, "woo": []})

    llamado = {"get": False}
    def _fallar_si_se_llama(*a, **k):
        llamado["get"] = True
        raise AssertionError("no debería llamar a Stock-Service con caché vigente")
    monkeypatch.setattr(client._http, "get", _fallar_si_se_llama)

    resultado = client.obtener_producto("ML000275")

    assert resultado == {"sku": "ML000275", "sap": {}, "woo": []}
    assert llamado["get"] is False


def test_obtener_producto_sin_cache_llama_a_la_api_y_guarda(monkeypatch):
    monkeypatch.setattr(client, "_leer_cache", lambda sku: None)

    guardado = {}
    def _guardar_falso(sku, datos):
        guardado["sku"] = sku
        guardado["datos"] = datos
    monkeypatch.setattr(client, "_guardar_cache", _guardar_falso)

    datos_api = {"sku": "ML000275", "sap": {"name": "Test"}, "woo": [{"woo_id": 1, "sync_warehouse": "01"}]}
    monkeypatch.setattr(client._http, "get", lambda *a, **k: _Respuesta(datos_api))

    resultado = client.obtener_producto("ML000275")

    assert resultado == datos_api
    assert guardado == {"sku": "ML000275", "datos": datos_api}


def test_obtener_producto_sku_inexistente_devuelve_sap_none_y_woo_vacio(monkeypatch):
    """Stock-Service no usa 404 para 'no encontrado' — siempre 200 con sap=None, woo=[]."""
    monkeypatch.setattr(client, "_leer_cache", lambda sku: None)
    monkeypatch.setattr(client, "_guardar_cache", lambda sku, datos: None)
    datos_api = {"sku": "NOEXISTE", "sap": None, "woo": [], "recent_logs": []}
    monkeypatch.setattr(client._http, "get", lambda *a, **k: _Respuesta(datos_api))

    resultado = client.obtener_producto("NOEXISTE")

    assert resultado["sap"] is None
    assert resultado["woo"] == []
