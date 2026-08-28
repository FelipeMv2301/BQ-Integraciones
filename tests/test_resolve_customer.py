"""Tests de app.pipelines.customers — con SQLite en memoria, sin red."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.models.reference_data import Industry, Municipality
from app.models.sap_customer import SAPCustomer
from app.models.woo_order import WooOrder
from app.pipelines import customers as pipeline
from app.pipelines.customers import PermanentError, TransientError


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


_DATOS_CLIENTE = {
    "name": "Cliente Prueba",
    "phone": "+56911111111",
    "email": "cliente@example.com",
    "business_activity": "Comercio",
    "industry_sap_code": "620",
    "contact_first_name": "Juan",
    "contact_last_name": "Perez",
    "contact_phone": "+56922222222",
    "contact_email": "contacto@example.com",
    "bill_address": "Calle Falsa 123",
    "bill_municipality_name": "SANTIAGO",
    "bill_municipality_city_name": "SANTIAGO",
    "bill_municipality_state_code": "13",
    "ship_address": "Calle Falsa 456",
    "ship_municipality_name": "NUNOA",
    "ship_municipality_city_name": "NUNOA",
    "ship_municipality_state_code": "13",
}


async def test_rut_invalido_lanza_permanent_error_sin_llamar_a_sap(session, monkeypatch):
    llamado = {"find_by_rut": False}
    monkeypatch.setattr(
        pipeline.sap_customers, "find_by_rut",
        lambda *a, **k: llamado.__setitem__("find_by_rut", True),
    )

    with pytest.raises(PermanentError):
        await pipeline.resolve_customer(session, "no-es-un-rut", _DATOS_CLIENTE)

    assert llamado["find_by_rut"] is False


async def test_cliente_nuevo_hace_post_y_queda_completed(session, monkeypatch):
    monkeypatch.setattr(pipeline.sap_customers, "find_by_rut", lambda rut: [])

    llamadas = []

    class _Respuesta:
        ok = True
        status_code = 201

    def _create_or_update_falso(existe, payload, code=None):
        llamadas.append({"existe": existe, "payload": payload, "code": code})
        return _Respuesta()

    monkeypatch.setattr(pipeline.sap_customers, "create_or_update", _create_or_update_falso)

    cliente = await pipeline.resolve_customer(session, "12345678-5", _DATOS_CLIENTE)

    assert cliente.status == "COMPLETED"
    assert cliente.exists is False
    assert cliente.code == "CN12345678-5"
    assert llamadas[0]["existe"] is False
    assert llamadas[0]["payload"]["CardCode"] == "CN12345678-5"


async def test_cliente_existente_reutiliza_contacto_y_direcciones(session, monkeypatch):
    resultado_sap = [{
        "CardCode": "CN12345678-5",
        "ContactEmployees": [{"InternalCode": 999, "Name": "CONTACTO", "Remarks1": "WEB"}],
        "BPAddresses": [
            {"RowNum": 0, "AddressName": "FISCAL", "AddressType": "bo_BillTo", "AddressName2": "WEB"},
            {"RowNum": 1, "AddressName": "DESPACHO", "AddressType": "bo_ShipTo", "AddressName2": "WEB"},
        ],
    }]
    monkeypatch.setattr(pipeline.sap_customers, "find_by_rut", lambda rut: resultado_sap)

    llamadas = []

    class _Respuesta:
        ok = True
        status_code = 204

    def _create_or_update_falso(existe, payload, code=None):
        llamadas.append({"existe": existe, "code": code})
        return _Respuesta()

    monkeypatch.setattr(pipeline.sap_customers, "create_or_update", _create_or_update_falso)

    cliente = await pipeline.resolve_customer(session, "12345678-5", _DATOS_CLIENTE)

    assert cliente.exists is True
    assert cliente.contact_code == 999
    assert cliente.bill_row == 0
    assert cliente.ship_row == 1
    assert llamadas[0]["existe"] is True
    assert llamadas[0]["code"] == "CN12345678-5"


async def test_sap_rechaza_el_payload_marca_failed_y_lanza_transient_error(session, monkeypatch):
    monkeypatch.setattr(pipeline.sap_customers, "find_by_rut", lambda rut: [])

    class _Respuesta:
        ok = False
        status_code = 400
        text = "dato inválido"

    monkeypatch.setattr(
        pipeline.sap_customers, "create_or_update",
        lambda existe, payload, code=None: _Respuesta(),
    )

    with pytest.raises(TransientError):
        await pipeline.resolve_customer(session, "12345678-5", _DATOS_CLIENTE)

    from sqlmodel import select
    cliente = (
        await session.execute(select(SAPCustomer).where(SAPCustomer.tax_id == "12345678-5"))
    ).scalar_one()
    assert cliente.status == "FAILED"
    assert "400" in cliente.status_message


# ── construir_datos_cliente (glue Woo -> resolve_customer, E6) ─────────────

async def _seed_catalogo(session):
    session.add(Municipality(woo_code="CL_100", sap_code="165", name="PROVIDENCIA", city_name="SANTIAGO", state_code="13"))
    session.add(Municipality(woo_code="CL_200", sap_code="200", name="NUNOA", city_name="SANTIAGO", state_code="13"))
    session.add(Industry(woo_code="EDUCA", sap_code="AE1", name="Educación"))
    await session.commit()


def _woo_order(billing: dict, shipping: dict) -> WooOrder:
    base_billing = {
        "first_name": "Pablo", "last_name": "Ruiz", "company": "",
        "address_1": "Calle 1", "address_2": "Depto 2",
        "state": "CL_100", "email": "billing@example.com", "phone": "111",
        "business_activity": "Comercio", "industry_id": "EDUCA",
    }
    base_shipping = {
        "first_name": "Ana", "last_name": "Soto",
        "address_1": "Calle 3", "address_2": "Depto 4", "state": "CL_200",
    }
    base_billing.update(billing)
    base_shipping.update(shipping)
    return WooOrder(
        code=1, reference=100, total=1000,
        billing_address=base_billing, shipping_address=base_shipping, items=[],
    )


async def test_construir_datos_cliente_usa_company_si_existe(session):
    await _seed_catalogo(session)
    orden = _woo_order({"company": "Universidad de La Frontera"}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["name"] == "Universidad de La Frontera"


async def test_construir_datos_cliente_usa_nombre_completo_si_no_hay_company(session):
    await _seed_catalogo(session)
    orden = _woo_order({}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["name"] == "Pablo Ruiz"


async def test_construir_datos_cliente_contacto_de_shipping_email_phone_de_billing(session):
    await _seed_catalogo(session)
    orden = _woo_order({}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["contact_first_name"] == "Ana"
    assert datos["contact_last_name"] == "Soto"
    assert datos["contact_email"] == "billing@example.com"
    assert datos["contact_phone"] == "111"


async def test_construir_datos_cliente_direccion_sin_espacio_entre_medio(session):
    await _seed_catalogo(session)
    orden = _woo_order({}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["bill_address"] == "Calle 1Depto 2"
    assert datos["ship_address"] == "Calle 3Depto 4"


async def test_construir_datos_cliente_resuelve_comunas(session):
    await _seed_catalogo(session)
    orden = _woo_order({}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["bill_municipality_name"] == "PROVIDENCIA"
    assert datos["ship_municipality_name"] == "NUNOA"
    assert datos["industry_sap_code"] == "AE1"


async def test_comuna_sin_mapear_lanza_permanent_error(session):
    await _seed_catalogo(session)
    orden = _woo_order({"state": "CL_999"}, {})

    with pytest.raises(PermanentError):
        await pipeline.construir_datos_cliente(session, orden)


async def test_giro_sin_mapear_lanza_permanent_error(session):
    await _seed_catalogo(session)
    orden = _woo_order({"industry_id": "NO-EXISTE"}, {})

    with pytest.raises(PermanentError):
        await pipeline.construir_datos_cliente(session, orden)


async def test_sin_giro_en_pedido_no_falla(session):
    await _seed_catalogo(session)
    orden = _woo_order({"industry_id": None}, {})

    datos = await pipeline.construir_datos_cliente(session, orden)

    assert datos["industry_sap_code"] is None
