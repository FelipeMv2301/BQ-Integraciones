"""Tests de app.services.sap.customers."""

import requests

from app.services.sap import customers


class _Respuesta:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")


def test_find_by_rut_arma_el_filtro_correcto(monkeypatch):
    filtros_recibidos = []

    def _solicitar_falso(metodo, endpoint, params=None):
        filtros_recibidos.append((metodo, endpoint, params))
        return _Respuesta(data={"value": []})

    monkeypatch.setattr(customers.client, "solicitar", _solicitar_falso)

    customers.find_by_rut("70990700-K")

    metodo, endpoint, params = filtros_recibidos[0]
    assert metodo == "GET"
    assert endpoint == "BusinessPartners"
    assert "FederalTaxID eq '70990700-K'" in params["$filter"]
    assert "startswith(FederalTaxID,'70990700')" in params["$filter"]
    assert "CardType eq 'C'" in params["$filter"]
    assert "GroupCode eq 100" in params["$filter"]


def test_find_by_rut_sin_resultados_devuelve_lista_vacia(monkeypatch):
    monkeypatch.setattr(
        customers.client, "solicitar",
        lambda *a, **k: _Respuesta(data={"value": []}),
    )

    assert customers.find_by_rut("70990700-K") == []


def test_find_by_rut_devuelve_resultados_crudos_de_sap(monkeypatch):
    resultado_sap = [{"CardCode": "CN70990700-K", "FederalTaxID": "70990700-K"}]
    monkeypatch.setattr(
        customers.client, "solicitar",
        lambda *a, **k: _Respuesta(data={"value": resultado_sap}),
    )

    assert customers.find_by_rut("70990700-K") == resultado_sap


def test_find_by_rut_propaga_error_http(monkeypatch):
    monkeypatch.setattr(
        customers.client, "solicitar",
        lambda *a, **k: _Respuesta(status_code=500),
    )

    try:
        customers.find_by_rut("70990700-K")
        raise AssertionError("debería haber lanzado HTTPError")
    except requests.exceptions.HTTPError:
        pass
