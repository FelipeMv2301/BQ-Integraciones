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
