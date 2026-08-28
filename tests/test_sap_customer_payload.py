"""Tests de CustomerPayload.build() y create_or_update en app.services.sap.customers."""

from app.models.sap_customer import SAPCustomer
from app.services.sap import customers


def _cliente_completo() -> SAPCustomer:
    return SAPCustomer(
        tax_id="70990700-K",
        code="CN70990700-K",
        name="Cliente de Prueba",
        phone="+56911111111",
        email="cliente@example.com",
        business_activity="Comercio",
        industry_sap_code="620",
        contact_code=1,
        contact_name="CONTACTO",
        contact_first_name="Juan",
        contact_last_name="Perez",
        contact_phone="+56922222222",
        contact_email="contacto@example.com",
        bill_code="FISCAL",
        bill_row=0,
        bill_address="Calle Falsa 123",
        bill_municipality_name="SANTIAGO",
        bill_municipality_city_name="SANTIAGO",
        bill_municipality_state_code="13",
        ship_code="DESPACHO",
        ship_row=1,
        ship_address="Calle Falsa 456",
        ship_municipality_name="NUNOA",
        ship_municipality_city_name="NUNOA",
        ship_municipality_state_code="13",
    )


def test_build_mapea_campos_del_cliente_al_payload_sap():
    payload = customers.CustomerPayload.build(_cliente_completo())
    dumped = payload.model_dump(by_alias=True)

    assert dumped["CardCode"] == "CN70990700-K"
    assert dumped["CardName"] == "Cliente de Prueba"
    assert dumped["FederalTaxID"] == "70990700-K"
    assert dumped["CardType"] == "C"
    assert dumped["GroupCode"] == "100"
    assert dumped["SalesPersonCode"] == customers.SAP_SELLER_CODE
    assert len(dumped["BPAddresses"]) == 2
    assert dumped["BPAddresses"][0]["AddressType"] == "bo_BillTo"
    assert dumped["BPAddresses"][0]["County"] == "SANTIAGO"
    assert dumped["BPAddresses"][1]["AddressType"] == "bo_ShipTo"
    assert dumped["BPAddresses"][1]["County"] == "NUNOA"
    assert len(dumped["ContactEmployees"]) == 1
    assert dumped["ContactEmployees"][0]["FirstName"] == "Juan"


def test_build_sin_code_falla_explicito():
    """Si cliente.code es None, debe fallar fuerte (Pydantic), no mandar CardCode vacío."""
    cliente = _cliente_completo()
    cliente.code = None

    try:
        customers.CustomerPayload.build(cliente)
        raise AssertionError("debería haber fallado por CardCode faltante")
    except Exception:
        pass


class _Respuesta:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_create_or_update_usa_post_cuando_no_existe(monkeypatch):
    llamadas = []

    def _solicitar_falso(metodo, endpoint, json_body=None):
        llamadas.append((metodo, endpoint, json_body))
        return _Respuesta()

    monkeypatch.setattr(customers.client, "solicitar", _solicitar_falso)

    customers.create_or_update(existe=False, payload={"CardCode": "CN123"})

    metodo, endpoint, json_body = llamadas[0]
    assert metodo == "POST"
    assert endpoint == "BusinessPartners"
    assert json_body == {"CardCode": "CN123"}


def test_create_or_update_usa_patch_y_code_en_url_cuando_existe(monkeypatch):
    llamadas = []

    def _solicitar_falso(metodo, endpoint, json_body=None):
        llamadas.append((metodo, endpoint, json_body))
        return _Respuesta()

    monkeypatch.setattr(customers.client, "solicitar", _solicitar_falso)

    customers.create_or_update(existe=True, payload={"CardCode": "CN123"}, code="CN123")

    metodo, endpoint, json_body = llamadas[0]
    assert metodo == "PATCH"
    assert endpoint == "BusinessPartners('CN123')"
