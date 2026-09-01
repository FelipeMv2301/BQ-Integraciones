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


def _pedido_crudo(id_: int = 1001, number: int = 900, tax_id: str = "12345678-5") -> dict:
    return {
        "id": id_,
        "number": number,
        "status": "processing",
        "date_paid_gmt": "2026-08-13T10:00:00",
        "total": "15000.00",
        "discount_total": "1000.00",
        "shipping_total": "2000.00",
        "pay_authorization_code": "AUTH123",
        "transaction_id": "",
        "shipping_lines": [{"method_id": "flat_rate", "method_title": "Despacho"}],
        "billing": {"first_name": "Juan", "tax_id": tax_id, "document_type": "33"},
        "shipping": {"first_name": "Juan", "address_1": "Calle 1"},
        "line_items": [{"product_id": 5, "quantity": 2, "total": 7000}],
    }


def test_extraer_pay_auth_code_prioriza_authorization_sobre_transaction():
    pedido = _pedido_crudo()
    assert pipeline._extraer_pay_auth_code(pedido) == "AUTH123"


def test_extraer_pay_auth_code_usa_transaction_id_si_no_hay_authorization():
    pedido = _pedido_crudo()
    pedido["pay_authorization_code"] = ""
    pedido["transaction_id"] = "TXN456"
    assert pipeline._extraer_pay_auth_code(pedido) == "TXN456"


def test_extraer_metodo_entrega_toma_la_primera_linea():
    pedido = _pedido_crudo()
    assert pipeline._extraer_metodo_entrega(pedido) == "flat_rate"


def test_extraer_metodo_entrega_sin_lineas_es_none():
    pedido = _pedido_crudo()
    pedido["shipping_lines"] = []
    assert pipeline._extraer_metodo_entrega(pedido) is None


def test_extraer_tax_id_y_doc_type_code():
    pedido = _pedido_crudo()
    assert pipeline._extraer_tax_id(pedido) == "12345678-5"
    assert pipeline._extraer_doc_type_code(pedido) == "33"


def test_extraer_montos_convierte_a_entero():
    pedido = _pedido_crudo()
    assert pipeline._extraer_total(pedido) == 15000
    assert pipeline._extraer_discount(pedido) == 1000
    assert pipeline._extraer_shipping(pedido) == 2000


def test_pedido_a_woo_order_mapea_todos_los_campos():
    orden = pipeline._pedido_a_woo_order(_pedido_crudo())
    assert orden.code == 1001
    assert orden.reference == 900
    assert orden.customer_tax_id == "12345678-5"
    assert orden.billing_address["first_name"] == "Juan"
    assert len(orden.items) == 1


async def test_poll_woo_orders_guarda_pedidos_nuevos(session, monkeypatch):
    pedidos = [_pedido_crudo(id_=1), _pedido_crudo(id_=2)]
    monkeypatch.setattr(pipeline.woo_orders_api, "obtener_pedidos", lambda modified_after=None: pedidos)

    resultado = await pipeline.poll_woo_orders(session)

    assert resultado == {"traidos": 2, "nuevos": 2, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_woo_orders_no_duplica_pedidos_ya_guardados(session, monkeypatch):
    session.add(pipeline._pedido_a_woo_order(_pedido_crudo(id_=1)))
    await session.commit()

    pedidos = [_pedido_crudo(id_=1), _pedido_crudo(id_=2)]
    monkeypatch.setattr(pipeline.woo_orders_api, "obtener_pedidos", lambda modified_after=None: pedidos)

    resultado = await pipeline.poll_woo_orders(session)

    assert resultado == {"traidos": 2, "nuevos": 1, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_woo_orders_marca_alerta_volumen_pero_no_aborta(session, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "MAX_ORDERS_PER_CYCLE", 1)
    pedidos = [_pedido_crudo(id_=1), _pedido_crudo(id_=2), _pedido_crudo(id_=3)]
    monkeypatch.setattr(pipeline.woo_orders_api, "obtener_pedidos", lambda modified_after=None: pedidos)

    resultado = await pipeline.poll_woo_orders(session)

    assert resultado["alerta_volumen"] is True
    assert resultado["nuevos"] == 3  # se procesan igual, no se aborta


# ── BioCommerce PRO (sitio nuevo, bioquimica.devwebs.cl) ────────────────

def _payload_biocommerce(id_: int = 9232, number: str = "9232", tax_id: str = "19.720.592-K") -> dict:
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


def test_bc_extraer_paid_at_parsea_iso():
    payload = _payload_biocommerce()
    payload["payment"]["paid_at"] = "2026-08-31T20:00:00+00:00"
    assert pipeline._bc_extraer_paid_at(payload) is not None


def test_bc_extraer_paid_at_none_si_no_pagado():
    assert pipeline._bc_extraer_paid_at(_payload_biocommerce()) is None


def test_bc_extraer_pay_auth_code_toma_transaction_id():
    payload = _payload_biocommerce()
    payload["payment"]["transaction_id"] = "TXN789"
    assert pipeline._bc_extraer_pay_auth_code(payload) == "TXN789"


def test_bc_extraer_pay_auth_code_none_si_vacio():
    assert pipeline._bc_extraer_pay_auth_code(_payload_biocommerce()) is None


def test_bc_extraer_delivery_method_code_usa_courier_code_no_method_id():
    assert pipeline._bc_extraer_delivery_method_code(_payload_biocommerce()) == "BIODEMO"


def test_bc_extraer_doc_type_code_convierte_sii_code_a_string():
    assert pipeline._bc_extraer_doc_type_code(_payload_biocommerce()) == "33"


def test_bc_billing_address_usa_comuna_code_no_state_legible():
    billing = pipeline._bc_billing_address(_payload_biocommerce())
    assert billing["state"] == "CL_114"
    assert billing["industry_id"] == "ALIM"
    assert billing["business_activity"] == "Alimentos"
    assert billing["company"] == "razon social prueba"


def test_bc_shipping_address_usa_comuna_code():
    shipping = pipeline._bc_shipping_address(_payload_biocommerce())
    assert shipping["state"] == "CL_114"


def test_bc_items_mapea_campos_del_producto():
    items = pipeline._bc_items(_payload_biocommerce())
    assert items == [
        {"sku": "RP0436B3", "product_id": 9210, "quantity": 1, "price": 9446, "total": 9446, "total_tax": 1795},
    ]


def test_pedido_biocommerce_a_woo_order_mapea_todos_los_campos():
    orden = pipeline._pedido_biocommerce_a_woo_order(_payload_biocommerce())
    assert orden.code == 9232
    assert orden.reference == 9232
    assert orden.total == 15231
    assert orden.shipping == 3990
    assert orden.customer_tax_id == "19.720.592-K"
    assert orden.bill_doc_type_code == "33"
    assert orden.delivery_method_code == "BIODEMO"
    assert orden.billing_address["state"] == "CL_114"
    assert len(orden.items) == 1


async def test_poll_biocommerce_orders_guarda_pedidos_nuevos(session, monkeypatch):
    pedidos = [_payload_biocommerce(id_=1), _payload_biocommerce(id_=2)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_biocommerce_orders(session, "2026-08-01", "2026-09-01")

    assert resultado == {"traidos": 2, "nuevos": 2, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_biocommerce_orders_no_duplica_pedidos_ya_guardados(session, monkeypatch):
    session.add(pipeline._pedido_biocommerce_a_woo_order(_payload_biocommerce(id_=1)))
    await session.commit()

    pedidos = [_payload_biocommerce(id_=1), _payload_biocommerce(id_=2)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_biocommerce_orders(session, "2026-08-01", "2026-09-01")

    assert resultado == {"traidos": 2, "nuevos": 1, "alerta_volumen": False}
    guardados = (await session.execute(select(WooOrder))).scalars().all()
    assert len(guardados) == 2


async def test_poll_biocommerce_orders_marca_alerta_volumen_pero_no_aborta(session, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "MAX_ORDERS_PER_CYCLE", 1)
    pedidos = [_payload_biocommerce(id_=1), _payload_biocommerce(id_=2), _payload_biocommerce(id_=3)]
    monkeypatch.setattr(
        pipeline.biocommerce_api, "obtener_pedidos",
        lambda date_from, date_to, status=None: pedidos,
    )

    resultado = await pipeline.poll_biocommerce_orders(session, "2026-08-01", "2026-09-01")

    assert resultado["alerta_volumen"] is True
    assert resultado["nuevos"] == 3
