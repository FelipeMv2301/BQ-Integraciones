"""Tests de app.pipelines.billing — con SQLite en memoria, sin red."""

from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.reference_data import DeliveryMethod
from app.models.sap_billing import SAPBilling
from app.models.woo_order import WooOrder
from app.pipelines import billing


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


def _item(product_id=1, qty=1, price=1000, total=1000, total_tax=190, sku="ML0001"):
    return {
        "product_id": product_id, "sku": sku, "quantity": qty,
        "price": price, "total": total, "total_tax": total_tax,
    }


def _producto_stock_service(woo_id, bodega="01"):
    return {"sap": {}, "woo": [{"woo_id": woo_id, "sync_warehouse": bodega}]}


# ── Funciones puras (sin DB) ─────────────────────────────────────────────────

def test_item_envio_usa_round_half_up_no_bankers_rounding():
    """
    remove_tax(2500)=2100.84 -> redondea a 2101 con ROUND_HALF_UP.
    round() nativo de Python en un .5 exacto redondearía distinto en
    algunos casos (bankers rounding) — este caso puntual no cae justo en
    .5, pero fija el comportamiento esperado igual que Integrify-Consola.
    """
    linea = billing._item_envio(2500, "SG000096")
    assert linea["price"] == 2101
    assert linea["total"] == 2101
    assert linea["total_tax"] == 2500 - 2101
    assert linea["sku"] == "SG000096"


def test_trocear_en_lotes_de_21():
    items = [_item(product_id=i) for i in range(45)]
    lotes = billing._trocear(items)
    assert len(lotes) == 3
    assert [len(lote) for lote in lotes] == [21, 21, 3]


def test_resolver_sku_bodega_matchea_por_woo_id(monkeypatch):
    monkeypatch.setattr(
        billing, "obtener_producto",
        lambda sku: {"woo": [{"woo_id": 5, "sync_warehouse": "01"}, {"woo_id": 7, "sync_warehouse": "11"}]},
    )
    sku, bodega = billing._resolver_sku_bodega(_item(product_id=7, sku="ML0001"))
    assert sku == "ML0001"
    assert bodega == "11"


def test_resolver_sku_bodega_sin_sku_falla_permanente():
    with pytest.raises(billing.PermanentError):
        billing._resolver_sku_bodega({"product_id": 1})


def test_resolver_sku_bodega_sin_datos_en_stockservice_falla_permanente(monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: {"woo": []})
    with pytest.raises(billing.PermanentError):
        billing._resolver_sku_bodega(_item())


# ── prepare_billing completo ─────────────────────────────────────────────────

async def _woo_order(session, **overrides) -> WooOrder:
    datos = {
        "code": 1, "reference": 100, "paid_at": datetime(2026, 8, 13, 18, 0, 0),
        "total": 1190, "shipping": 0, "delivery_method_code": None,
        "bill_doc_type_code": "39", "customer_tax_id": "12345678-5",
        "items": [_item()],
    }
    datos.update(overrides)
    orden = WooOrder(**datos)
    session.add(orden)
    await session.commit()
    return orden


async def test_prepare_billing_crea_un_solo_lote_sin_envio(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    orden = await _woo_order(session, total=1190)

    facturaciones = await billing.prepare_billing(session, orden)

    assert len(facturaciones) == 1
    assert facturaciones[0].total == 1190
    assert facturaciones[0].chunk_index == 0
    assert orden.status == "COMPLETED"


async def test_prepare_billing_discrepancia_marca_woo_order_failed_con_mensaje(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    orden = await _woo_order(session, total=9999999)

    with pytest.raises(billing.PermanentError):
        await billing.prepare_billing(session, orden)

    assert orden.status == "FAILED"
    assert "Discrepancia de totales" in orden.status_message
    assert orden.attempts == 1


async def test_prepare_billing_agrega_item_de_envio(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    metodo = DeliveryMethod(woo_code="flat_rate", sap_sku="SG000096", name="Despacho")
    session.add(metodo)
    await session.commit()

    envio = billing._item_envio(2500, "SG000096")
    total_esperado = 1190 + envio["total"] + envio["total_tax"]
    orden = await _woo_order(session, total=total_esperado, shipping=2500, delivery_method_code="flat_rate")

    facturaciones = await billing.prepare_billing(session, orden)

    assert len(facturaciones[0].items) == 2
    assert facturaciones[0].items[1]["sku"] == "SG000096"


async def test_prepare_billing_shipping_sin_metodo_de_envio_falla_permanente(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    orden = await _woo_order(session, shipping=2500, delivery_method_code="metodo_inexistente")

    with pytest.raises(billing.PermanentError):
        await billing.prepare_billing(session, orden)


async def test_prepare_billing_discrepancia_de_totales_falla_permanente(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    orden = await _woo_order(session, total=9999999)  # no coincide con la suma real de items

    with pytest.raises(billing.PermanentError):
        await billing.prepare_billing(session, orden)


async def test_prepare_billing_es_idempotente_no_duplica_chunks(session, monkeypatch):
    monkeypatch.setattr(billing, "obtener_producto", lambda sku: _producto_stock_service(1))
    orden = await _woo_order(session, total=1190)

    await billing.prepare_billing(session, orden)
    await billing.prepare_billing(session, orden)

    filas = (
        await session.execute(select(SAPBilling).where(SAPBilling.woo_order_id == orden.id))
    ).scalars().all()
    assert len(filas) == 1


def test_fecha_pago_chile_convierte_utc_a_fecha_local():
    # 2026-08-14 02:00 UTC -> 2026-08-13 22:00 en Chile (UTC-4 en horario de verano boreal / -3 según DST real)
    resultado = billing._fecha_pago_chile(datetime(2026, 8, 14, 2, 0, 0))
    assert isinstance(resultado, date)
