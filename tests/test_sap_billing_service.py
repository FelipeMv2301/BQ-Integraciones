"""Tests de app.services.sap.billing::buscar_factura_existente (BQI-37)."""

from app.services.sap import billing


class _Respuesta:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_buscar_factura_existente_devuelve_none_si_no_hay_match(monkeypatch):
    monkeypatch.setattr(billing.client, "solicitar", lambda *a, **kw: _Respuesta({"value": []}))
    assert billing.buscar_factura_existente(order_num=100, total=1190, doc_type_code="39") is None


def test_buscar_factura_existente_devuelve_el_match(monkeypatch):
    monkeypatch.setattr(
        billing.client, "solicitar",
        lambda *a, **kw: _Respuesta({"value": [{"DocEntry": 555, "DocNum": 999}]}),
    )
    resultado = billing.buscar_factura_existente(order_num=100, total=1190, doc_type_code="39")
    assert resultado == {"DocEntry": 555, "DocNum": 999}


def test_buscar_factura_existente_manda_order_num_entre_comillas(monkeypatch):
    """
    Regresión: U_WedDocNum es un campo string en SAP pese a representar un
    número. Sin comillas, SAP Service Layer devuelve 400 ("the given value
    is not a string") — confirmado contra SAP real el 2026-08-17. Si esto
    vuelve a romperse, es exactamente este bug de nuevo.
    """
    capturado = {}

    def fake_solicitar(metodo, endpoint, params=None, **kw):
        capturado.update(params)
        return _Respuesta({"value": []})

    monkeypatch.setattr(billing.client, "solicitar", fake_solicitar)
    billing.buscar_factura_existente(order_num=100, total=1190, doc_type_code="39")

    assert "U_WedDocNum eq '100'" in capturado["$filter"]
