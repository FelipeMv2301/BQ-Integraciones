"""Tests de app.pipelines.woo_orders — con SQLite en memoria, sin red."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.woo_order import WooOrder
from app.pipelines import woo_orders as pipeline


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


def _payload(id_: int = 9232, number: str = "9232", tax_id: str = "19.720.592-K") -> dict:
    return {
        "order": {
            "id": id_, "number": number, "status": "on-hold",
            "created_at": "2026-08-31T18:01:28+00:00",
        },
        "tax_document": {
            "type": "factura", "sii_code": 33, "tax_id": tax_id,
            "business_name": "razon social prueba", "business_activity": "Alimentos",
            "business_activity_code": "ALIM",
        },
        "billing_address": {
            "first_name": "razon social prueba", "last_name": "", "company": "razon social prueba",
            "address_1": "camino padre hurtado 6510", "address_2": "",
            "state": "Metropolitana de Santiago", "comuna": "Buin", "comuna_code": "CL_114",
            "phone": "+56951306275", "email": "angelo@piso29.cl",
        },
        "shipping_address": {
            "first_name": "razon social prueba", "last_name": "",
            "address_1": "camino padre hurtado 6510", "address_2": "",
            "state": "Metropolitana de Santiago", "comuna": "Buin", "comuna_code": "CL_114",
        },
        "products": [
            {"item_id": 4, "product_id": 9210, "sku": "RP0436B3", "quantity": 1,
             "unit_price": 9446, "total_before_tax": 9446, "tax": 1795},
        ],
        "totals": {"currency": "CLP", "subtotal": 9446, "discount_total": 0, "shipping_total": 3990, "total": 15231},
        "shipping": {"total": 3990, "courier": "Courier de ejemplo", "courier_code": "BIODEMO", "lines": []},
        "payment": {"method": "bacs", "transaction_id": None, "paid": False, "paid_at": None},
        "integration": {"courier_code": "BIODEMO"},
    }


def test_extraer_paid_at_parsea_iso():
    payload = _payload()
    payload["payment"]["paid_at"] = "2026-08-31T20:00:00+00:00"
    assert pipeline._extraer_paid_at(payload) is not None


def test_extraer_paid_at_none_si_no_pagado():
    assert pipeline._extraer_paid_at(_payload()) is None


def test_extraer_paid_at_normaliza_a_utc_naive():
    """Bug real 2026-09-02: payment.paid_at llega con offset -- la columna
    es TIMESTAMP WITHOUT TIME ZONE, hay que despojar el tzinfo."""
    payload = _payload()
    payload["payment"]["paid_at"] = "2026-09-02T10:54:11+00:00"
    fecha = pipeline._extraer_paid_at(payload)
    assert fecha.tzinfo is None
    assert fecha.hour == 10


def test_extraer_pay_auth_code_toma_transaction_id():
    payload = _payload()
    payload["payment"]["transaction_id"] = "TXN789"
    assert pipeline._extraer_pay_auth_code(payload) == "TXN789"


def test_extraer_pay_auth_code_none_si_vacio():
    assert pipeline._extraer_pay_auth_code(_payload()) is None


def test_extraer_delivery_method_code_usa_courier_code_no_method_id():
    assert pipeline._extraer_delivery_method_code(_payload()) == "BIODEMO"


def test_extraer_doc_type_code_convierte_sii_code_a_string():
    assert pipeline._extraer_doc_type_code(_payload()) == "33"


def test_extraer_tax_id_normaliza_puntos():
    """Bug real 2026-09-02: create_sap_invoice comparaba tax_id normalizado
    (guardado por resolve_customer) contra customer_tax_id crudo -- nunca
    encontraba al cliente ya resuelto. Se normaliza en la ingesta."""
    assert pipeline._extraer_tax_id(_payload(tax_id="19.720.592-k")) == "19720592-K"


def test_normalizar_tax_id_ingesta_invalido_se_guarda_crudo():
    """RUT inválido no explota la ingesta del lote (I2) -- resolve_customer
    lo sigue rechazando después, por pedido."""
    assert pipeline._normalizar_tax_id_ingesta("no-es-un-rut") == "no-es-un-rut"
    assert pipeline._normalizar_tax_id_ingesta(None) is None


def test_extraer_orden_compra_presente():
    """tax_document.orden_compra (2026-09-04) -- confirmado en vivo contra
    un pedido real (9238) que BioCommerce lo expone ahí."""
    payload = _payload()
    payload["tax_document"]["orden_compra"] = "OC-2026-001"
    assert pipeline._extraer_orden_compra(payload) == "OC-2026-001"


def test_extraer_orden_compra_ausente_o_vacia_es_none():
    assert pipeline._extraer_orden_compra(_payload()) is None
    payload = _payload()
    payload["tax_document"]["orden_compra"] = ""
    assert pipeline._extraer_orden_compra(payload) is None
    payload["tax_document"]["orden_compra"] = None
    assert pipeline._extraer_orden_compra(payload) is None


def test_billing_address_usa_comuna_code_no_state_legible():
    billing = pipeline._billing_address(_payload())
    assert billing["state"] == "CL_114"
    assert billing["industry_id"] == "ALIM"
    assert billing["business_activity"] == "Alimentos"
    assert billing["company"] == "razon social prueba"


def test_shipping_address_usa_comuna_code():
    shipping = pipeline._shipping_address(_payload())
    assert shipping["state"] == "CL_114"


def test_extraer_items_mapea_campos_del_producto():
    items = pipeline._extraer_items(_payload())
    assert items == [
        {"sku": "RP0436B3", "product_id": 9210, "quantity": 1, "price": 9446, "total": 9446, "total_tax": 1795},
    ]


def test_pedido_a_woo_order_mapea_todos_los_campos():
    orden = pipeline._pedido_a_woo_order(_payload())
    assert orden.code == 9232
    assert orden.reference == 9232
    assert orden.total == 15231
    assert orden.shipping == 3990
    assert orden.customer_tax_id == "19720592-K"  # normalizado en la ingesta (sin puntos)
    assert orden.bill_doc_type_code == "33"
    assert orden.delivery_method_code == "BIODEMO"
    assert orden.billing_address["state"] == "CL_114"
    assert len(orden.items) == 1


async def test_poll_woo_orders_guarda_pedidos_nuevos(session, monkeypatch):
    pedidos = [_payload(id_=1), _payload(id_=2)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_woo_orders(session, "2026-08-01", "2026-09-01")

    assert resultado == {"traidos": 2, "nuevos": 2, "fallidos": 0, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_woo_orders_no_duplica_pedidos_ya_guardados(session, monkeypatch):
    session.add(pipeline._pedido_a_woo_order(_payload(id_=1)))
    await session.commit()

    pedidos = [_payload(id_=1), _payload(id_=2)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_woo_orders(session, "2026-08-01", "2026-09-01")

    assert resultado == {"traidos": 2, "nuevos": 1, "fallidos": 0, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_woo_orders_marca_alerta_volumen_pero_no_aborta(session, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "MAX_ORDERS_PER_CYCLE", 1)
    pedidos = [_payload(id_=1), _payload(id_=2), _payload(id_=3)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_woo_orders(session, "2026-08-01", "2026-09-01")

    assert resultado["alerta_volumen"] is True
    assert resultado["nuevos"] == 3  # se procesan igual, no se aborta


async def test_poll_woo_orders_pedido_malformado_no_bloquea_a_los_demas(session, monkeypatch):
    """
    Bug real (auditoría 2026-09-02): sin try/except por pedido, uno solo
    malformado lanzaba ANTES del commit() y NINGÚN pedido del ciclo se
    guardaba, ni siquiera los válidos -- Integrify-Consola (legado) sí
    aísla por pedido, acá no se igualaba ese aislamiento (I2).
    """
    pedido_malo = _payload(id_=2)
    del pedido_malo["totals"]["total"]  # rompe _pedido_a_woo_order (KeyError)
    pedidos = [_payload(id_=1), pedido_malo, _payload(id_=3)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_woo_orders(session, "2026-08-01", "2026-09-01")

    assert resultado == {"traidos": 3, "nuevos": 2, "fallidos": 1, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert {g.code for g in guardados} == {1, 3}
