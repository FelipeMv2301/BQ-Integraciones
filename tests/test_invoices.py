"""Tests de app.pipelines.invoices — con SQLite en memoria, sin red."""

from datetime import date

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.pipelines import invoices


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


async def _armar_facturacion(session, doc_entry=555, tax_id="12345678-5") -> SAPBilling:
    orden = WooOrder(code=1, reference=100, total=1000, customer_tax_id=tax_id, items=[])
    session.add(orden)
    await session.commit()

    factura = SAPBilling(
        woo_order_id=orden.id, chunk_index=0, doc_type_code="39", total=1000,
        doc_date=date(2026, 8, 13), items=[], doc_entry=doc_entry, doc_num=999,
        status="COMPLETED",
    )
    session.add(factura)
    await session.commit()
    return factura


async def test_sin_folio_todavia_no_crea_factura_ni_falla(session, monkeypatch):
    await _armar_facturacion(session)
    monkeypatch.setattr(invoices, "_consultar_folio", lambda doc_entry: None)

    resultado = await invoices.poll_sap_invoices(session)

    assert resultado == {"pendientes": 1, "nuevas": 0}
    filas = (await session.execute(select(SAPInvoice))).scalars().all()
    assert len(filas) == 0


async def test_con_folio_crea_sap_invoice_con_emails_del_cliente(session, monkeypatch):
    await _armar_facturacion(session, doc_entry=555, tax_id="12345678-5")
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", email="cliente@example.com",
        contact_email="contacto@example.com",
    ))
    await session.commit()

    monkeypatch.setattr(
        invoices, "_consultar_folio",
        lambda doc_entry: {"DocEntry": doc_entry, "DocNum": 999, "FolioNumber": 42742, "FolioPrefixString": "33"},
    )

    resultado = await invoices.poll_sap_invoices(session)

    assert resultado == {"pendientes": 1, "nuevas": 1}
    factura = (await session.execute(select(SAPInvoice))).scalar_one()
    assert factura.folio == 42742
    assert factura.customer_email == "cliente@example.com"
    assert factura.contact_email == "contacto@example.com"


async def test_no_duplica_si_ya_tiene_sap_invoice(session, monkeypatch):
    factura_billing = await _armar_facturacion(session, doc_entry=555)
    session.add(SAPInvoice(sap_billing_id=factura_billing.id, doc_entry=555, folio=42742))
    await session.commit()

    llamado = {"consultar": False}
    def _fallar_si_se_llama(doc_entry):
        llamado["consultar"] = True
        raise AssertionError("no debería consultar de nuevo un doc_entry ya con factura")
    monkeypatch.setattr(invoices, "_consultar_folio", _fallar_si_se_llama)

    resultado = await invoices.poll_sap_invoices(session)

    assert resultado == {"pendientes": 0, "nuevas": 0}
    assert llamado["consultar"] is False
