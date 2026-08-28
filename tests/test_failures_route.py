"""Tests de app.api.routes.failures (BQI-61) — con SQLite en memoria, sin red."""

from datetime import date

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.api.routes import failures
from app.core.config import settings
from app.models.email import Email
from app.models.enums import EmailEventType
from app.models.failure import Failure
from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.pipelines import documents


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


# ── GET /failures ────────────────────────────────────────────────────────

async def test_listar_failures_devuelve_todas_las_filas(session):
    session.add(Failure(entity_type="WooOrder", entity_id=1, stage="prepare_billing", error_message="x", attempts=3))
    session.add(Failure(entity_type="Email", entity_id=2, stage="send_email", error_message="y", attempts=1))
    await session.commit()

    resultado = await failures.listar_failures(session)

    assert len(resultado) == 2
    assert {f["entity_type"] for f in resultado} == {"WooOrder", "Email"}


# ── POST /retry — validaciones ──────────────────────────────────────────

async def test_retry_tabla_desconocida_da_404(session):
    with pytest.raises(HTTPException) as exc:
        await failures.reintentar("no_existe", 1, session)
    assert exc.value.status_code == 404


async def test_retry_id_inexistente_da_404(session):
    with pytest.raises(HTTPException) as exc:
        await failures.reintentar("woo_orders", 999, session)
    assert exc.value.status_code == 404


async def test_retry_ya_completed_da_409(session):
    orden = WooOrder(code=1, reference=100, total=0, items=[], bill_doc_type_code="39", status="COMPLETED")
    session.add(orden)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await failures.reintentar("woo_orders", orden.id, session)
    assert exc.value.status_code == 409


# ── POST /retry — camino feliz por tabla (pipeline mockeado) ────────────

async def test_retry_woo_order_llama_prepare_billing(session, monkeypatch):
    orden = WooOrder(code=1, reference=100, total=0, items=[], bill_doc_type_code="39", status="FAILED")
    session.add(orden)
    await session.commit()

    llamados = []

    async def _fake_prepare_billing(s, woo_order):
        llamados.append(woo_order.id)
        woo_order.status = "COMPLETED"

    monkeypatch.setattr(failures.billing, "prepare_billing", _fake_prepare_billing)

    resultado = await failures.reintentar("woo_orders", orden.id, session)

    assert llamados == [orden.id]
    assert resultado["status"] == "COMPLETED"


async def test_retry_sap_customer_reconstruye_datos_desde_woo_order(session, monkeypatch):
    orden = WooOrder(
        code=1, reference=100, total=0, items=[], bill_doc_type_code="39",
        customer_tax_id="12345678-5",
    )
    session.add(orden)
    cliente = SAPCustomer(tax_id="12345678-5", status="FAILED")
    session.add(cliente)
    await session.commit()

    async def _fake_construir_datos_cliente(s, wo):
        return {"fake": "datos"}

    monkeypatch.setattr(failures.customers, "construir_datos_cliente", _fake_construir_datos_cliente)
    llamados = []

    async def _fake_resolve_customer(s, tax_id, datos):
        llamados.append((tax_id, datos))
        cliente.status = "COMPLETED"

    monkeypatch.setattr(failures.customers, "resolve_customer", _fake_resolve_customer)

    resultado = await failures.reintentar("sap_customers", cliente.id, session)

    assert llamados == [("12345678-5", {"fake": "datos"})]
    assert resultado["status"] == "COMPLETED"


async def test_retry_sap_customer_sin_woo_order_da_422(session):
    cliente = SAPCustomer(tax_id="99999999-9", status="FAILED")
    session.add(cliente)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await failures.reintentar("sap_customers", cliente.id, session)
    assert exc.value.status_code == 422


async def test_retry_sap_billing_sin_woo_order_da_422(session):
    """
    Regresión: antes de arreglar el except Exception de reintentar(), este
    422 quedaba silenciado y el endpoint devolvía 200 igual.
    """
    factura = SAPBilling(
        woo_order_id=999999, chunk_index=0, doc_type_code="39", total=0,
        doc_date=date(2026, 8, 13), internal_notes="x", public_notes="x", items=[], status="FAILED",
    )
    session.add(factura)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await failures.reintentar("sap_billings", factura.id, session)
    assert exc.value.status_code == 422


async def test_retry_sap_billing_llama_create_sap_invoice(session, monkeypatch):
    orden = WooOrder(code=1, reference=100, total=0, items=[], bill_doc_type_code="39")
    session.add(orden)
    await session.commit()
    factura = SAPBilling(
        woo_order_id=orden.id, chunk_index=0, doc_type_code="39", total=0,
        doc_date=date(2026, 8, 13), internal_notes="x", public_notes="x", items=[], status="FAILED",
    )
    session.add(factura)
    await session.commit()

    llamados = []

    async def _fake_create_sap_invoice(s, f, wo):
        llamados.append((f.id, wo.id))
        f.status = "COMPLETED"

    monkeypatch.setattr(failures.billing, "create_sap_invoice", _fake_create_sap_invoice)

    resultado = await failures.reintentar("sap_billings", factura.id, session)

    assert llamados == [(factura.id, orden.id)]
    assert resultado["status"] == "COMPLETED"


async def test_retry_sap_invoice_llama_fetch_pdf(session, monkeypatch):
    factura = SAPInvoice(doc_entry=1, folio=42742, doc_type_code="33", status="FAILED")
    session.add(factura)
    await session.commit()

    llamados = []

    async def _fake_fetch_pdf(s, f):
        llamados.append(f.id)
        f.status = "COMPLETED"

    monkeypatch.setattr(failures.documents, "fetch_pdf", _fake_fetch_pdf)

    resultado = await failures.reintentar("sap_invoices", factura.id, session)

    assert llamados == [factura.id]
    assert resultado["status"] == "COMPLETED"


async def test_retry_email_llama_send_email(session, monkeypatch):
    email = Email(event_type=EmailEventType.CUSTOMER_INVOICE.value, status="FAILED")
    session.add(email)
    await session.commit()

    llamados = []

    async def _fake_send_email(s, e):
        llamados.append(e.id)
        e.status = "COMPLETED"

    monkeypatch.setattr(failures.notifications, "send_email", _fake_send_email)

    resultado = await failures.reintentar("emails", email.id, session)

    assert llamados == [email.id]
    assert resultado["status"] == "COMPLETED"


async def test_retry_error_no_explota_devuelve_status_actualizado(session, monkeypatch):
    """
    Si la función de pipeline lanza, el endpoint no debe romper con 500 —
    reporta el status_message que la propia función ya dejó grabado antes
    de lanzar (I1/I6), no necesita distinguir el tipo de excepción.
    """
    factura = SAPInvoice(doc_entry=1, folio=42742, doc_type_code="33", status="FAILED")
    session.add(factura)
    await session.commit()

    async def _fake_fetch_pdf_falla(s, f):
        f.status, f.status_message = "FAILED", "Facele caído"
        raise documents.TransientError("Facele caído")

    monkeypatch.setattr(failures.documents, "fetch_pdf", _fake_fetch_pdf_falla)

    resultado = await failures.reintentar("sap_invoices", factura.id, session)

    assert resultado["status"] == "FAILED"
    assert resultado["status_message"] == "Facele caído"


async def test_retry_que_agota_intentos_escala_a_exhausted_y_notifica(session, monkeypatch):
    """I6: si el reintento manual también agota el límite, sube a EXHAUSTED,
    deja fila en failures, y dispara notify_failure."""
    monkeypatch.setattr(settings, "FACELE_MAX_ATTEMPTS", 1)
    factura = SAPInvoice(
        doc_entry=1, folio=42742, doc_type_code="33", status="FAILED", attempts=0,
    )
    session.add(factura)
    await session.commit()

    async def _fake_fetch_pdf_falla(s, f):
        f.status, f.status_message = "FAILED", "estado=0"
        f.attempts += 1
        raise documents.TransientError("estado=0")

    monkeypatch.setattr(failures.documents, "fetch_pdf", _fake_fetch_pdf_falla)
    llamados_notify = []
    monkeypatch.setattr(
        failures.failure_tracking, "notify_failure",
        lambda kind, msg: llamados_notify.append((kind, msg)),
    )

    resultado = await failures.reintentar("sap_invoices", factura.id, session)

    assert resultado["status"] == "EXHAUSTED"
    assert len(llamados_notify) == 1
    assert llamados_notify[0][0] == "SAPInvoice:fetch_pdf"

    filas = (await session.execute(select(Failure))).scalars().all()
    assert len(filas) == 1
    assert filas[0].entity_type == "SAPInvoice"
    assert filas[0].stage == "fetch_pdf"
