"""Tests de app.pipelines.orchestrator — con SQLite en memoria y todas las
llamadas externas mockeadas, sin red."""

from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.models.email import Email
from app.models.enums import EmailEventType
from app.models.sap_billing import SAPBilling
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.pipelines import orchestrator


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture(autouse=True)
def _sin_escalamiento_por_defecto(monkeypatch):
    """Los tests de este archivo prueban la orquestación, no el escalamiento
    a EXHAUSTED (eso ya tiene su propia suite en test_failure_tracking.py)."""
    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(orchestrator.failure_tracking, "escalar_si_agotado", _noop)


def _pedido_crudo(id_=1001, number=900, tax_id="12345678-5") -> dict:
    return {
        "id": id_, "number": number, "status": "processing",
        "date_paid_gmt": "2026-08-13T10:00:00", "total": "15000.00",
        "discount_total": "0", "shipping_total": "0",
        "pay_authorization_code": "", "transaction_id": "",
        "shipping_lines": [], "billing": {"tax_id": tax_id, "document_type": "39"},
        "shipping": {}, "line_items": [],
    }


async def _armar_woo_order(session, code=1001, tax_id="12345678-5", status="PENDING") -> WooOrder:
    orden = WooOrder(
        code=code, reference=900, total=15000, customer_tax_id=tax_id,
        items=[], bill_doc_type_code="39", status=status,
    )
    session.add(orden)
    await session.commit()
    return orden


async def _armar_sap_invoice(session, doc_entry=1, folio=42742, status="PENDING") -> SAPInvoice:
    factura = SAPInvoice(doc_entry=doc_entry, folio=folio, doc_type_code="33", status=status)
    session.add(factura)
    await session.commit()
    return factura


async def _armar_sap_billing(session, woo_order, chunk_index=0, status="PENDING") -> SAPBilling:
    factura = SAPBilling(
        woo_order_id=woo_order.id, chunk_index=chunk_index, doc_type_code="39", total=0,
        doc_date=date(2026, 8, 13), internal_notes="x", public_notes="x", items=[], status=status,
    )
    session.add(factura)
    await session.commit()
    return factura


def _mock_ok(monkeypatch):
    """Mockea las 4 llamadas externas del camino feliz — cada test ajusta lo que necesite."""
    async def _construir_datos(s, wo):
        return {"fake": "datos"}
    async def _resolve_customer(s, tax_id, datos):
        return SimpleNamespace(code=f"CN{tax_id}")
    async def _prepare_billing(s, wo):
        return [
            SimpleNamespace(chunk_index=0, status="PENDING", attempts=0, id=1),
            SimpleNamespace(chunk_index=1, status="PENDING", attempts=0, id=2),
        ]
    async def _create_sap_invoice(s, factura, wo):
        factura.status = "COMPLETED"
        return SimpleNamespace(chunk_index=factura.chunk_index, doc_entry=100 + factura.chunk_index, doc_num=5, status="COMPLETED")

    monkeypatch.setattr(orchestrator.customers, "construir_datos_cliente", _construir_datos)
    monkeypatch.setattr(orchestrator.customers, "resolve_customer", _resolve_customer)
    monkeypatch.setattr(orchestrator.billing, "prepare_billing", _prepare_billing)
    monkeypatch.setattr(orchestrator.billing, "create_sap_invoice", _create_sap_invoice)


# ── sync_order_to_sap (manual, un pedido puntual) ───────────────────────

async def test_pedido_ya_existe_no_llama_a_woocommerce(session, monkeypatch):
    await _armar_woo_order(session)
    _mock_ok(monkeypatch)
    monkeypatch.setattr(
        orchestrator.woo_orders_api, "obtener_pedido",
        lambda code: (_ for _ in ()).throw(AssertionError("no debería llamar a WooCommerce")),
    )

    resultado = await orchestrator.sync_order_to_sap(session, 1001)

    assert resultado["error"] is None
    assert resultado["cliente"] == "CN12345678-5"


async def test_pedido_no_existe_lo_trae_de_woocommerce(session, monkeypatch):
    monkeypatch.setattr(orchestrator.woo_orders_api, "obtener_pedido", lambda code: _pedido_crudo(id_=code))
    _mock_ok(monkeypatch)

    resultado = await orchestrator.sync_order_to_sap(session, 2002)

    assert resultado["error"] is None
    assert resultado["cliente"] == "CN12345678-5"


async def test_pedido_no_existe_en_woocommerce_devuelve_error_sin_seguir(session, monkeypatch):
    monkeypatch.setattr(orchestrator.woo_orders_api, "obtener_pedido", lambda code: None)
    llamado = []
    async def _resolve_customer(s, tax_id, datos):
        llamado.append(True)
    monkeypatch.setattr(orchestrator.customers, "resolve_customer", _resolve_customer)

    resultado = await orchestrator.sync_order_to_sap(session, 3003)

    assert "no existe en WooCommerce" in resultado["error"]
    assert llamado == []


# ── sync_order_to_sap_biocommerce (manual, sitio nuevo) ─────────────────

def _payload_biocommerce_crudo(id_=9232, number="9232", tax_id="19.720.592-K") -> dict:
    return {
        "order": {"id": id_, "number": number, "status": "on-hold"},
        "tax_document": {"sii_code": 33, "tax_id": tax_id, "business_name": "razon social prueba"},
        "billing_address": {"first_name": "razon social prueba", "comuna_code": "CL_114"},
        "shipping_address": {"comuna_code": "CL_114"},
        "products": [{"sku": "RP0436B3", "product_id": 9210, "quantity": 1, "unit_price": 9446,
                      "total_before_tax": 9446, "tax": 1795}],
        "totals": {"total": 15231, "discount_total": 0, "shipping_total": 3990},
        "shipping": {"courier_code": "BIODEMO"},
        "payment": {"transaction_id": None, "paid_at": None},
    }


async def test_biocommerce_pedido_ya_existe_no_llama_a_biocommerce(session, monkeypatch):
    await _armar_woo_order(session, code=9232)
    _mock_ok(monkeypatch)
    monkeypatch.setattr(
        orchestrator.biocommerce_api, "obtener_pedido",
        lambda code: (_ for _ in ()).throw(AssertionError("no debería llamar a BioCommerce")),
    )

    resultado = await orchestrator.sync_order_to_sap_biocommerce(session, 9232)

    assert resultado["error"] is None
    assert resultado["cliente"] == "CN12345678-5"


async def test_biocommerce_pedido_no_existe_lo_trae_de_biocommerce(session, monkeypatch):
    monkeypatch.setattr(
        orchestrator.biocommerce_api, "obtener_pedido",
        lambda code: _payload_biocommerce_crudo(id_=code),
    )
    _mock_ok(monkeypatch)

    resultado = await orchestrator.sync_order_to_sap_biocommerce(session, 9232)

    assert resultado["error"] is None
    assert resultado["cliente"] == "CN19.720.592-K"


async def test_biocommerce_pedido_no_existe_devuelve_error_sin_seguir(session, monkeypatch):
    monkeypatch.setattr(orchestrator.biocommerce_api, "obtener_pedido", lambda code: None)
    llamado = []
    async def _resolve_customer(s, tax_id, datos):
        llamado.append(True)
    monkeypatch.setattr(orchestrator.customers, "resolve_customer", _resolve_customer)

    resultado = await orchestrator.sync_order_to_sap_biocommerce(session, 9999)

    assert "no existe en BioCommerce" in resultado["error"]
    assert llamado == []


async def test_resolve_customer_falla_corta_antes_de_billing(session, monkeypatch):
    await _armar_woo_order(session)
    async def _construir_datos(s, wo):
        return {}
    async def _resolve_customer_falla(s, tax_id, datos):
        raise ValueError("RUT no válido")
    llamado_billing = []
    async def _prepare_billing(s, wo):
        llamado_billing.append(True)
        return []

    monkeypatch.setattr(orchestrator.customers, "construir_datos_cliente", _construir_datos)
    monkeypatch.setattr(orchestrator.customers, "resolve_customer", _resolve_customer_falla)
    monkeypatch.setattr(orchestrator.billing, "prepare_billing", _prepare_billing)

    resultado = await orchestrator.sync_order_to_sap(session, 1001)

    assert "resolve_customer" in resultado["error"]
    assert llamado_billing == []


async def test_prepare_billing_falla_corta_antes_de_crear_facturas(session, monkeypatch):
    await _armar_woo_order(session)
    _mock_ok(monkeypatch)
    async def _prepare_billing_falla(s, wo):
        raise ValueError("Discrepancia de totales")
    llamado_invoice = []
    async def _create_sap_invoice(s, factura, wo):
        llamado_invoice.append(True)

    monkeypatch.setattr(orchestrator.billing, "prepare_billing", _prepare_billing_falla)
    monkeypatch.setattr(orchestrator.billing, "create_sap_invoice", _create_sap_invoice)

    resultado = await orchestrator.sync_order_to_sap(session, 1001)

    assert "prepare_billing" in resultado["error"]
    assert llamado_invoice == []


async def test_camino_feliz_completo_devuelve_cliente_y_facturas(session, monkeypatch):
    await _armar_woo_order(session)
    _mock_ok(monkeypatch)

    resultado = await orchestrator.sync_order_to_sap(session, 1001)

    assert resultado["error"] is None
    assert resultado["cliente"] == "CN12345678-5"
    assert len(resultado["facturas"]) == 2
    assert resultado["facturas"][0] == {"chunk_index": 0, "doc_entry": 100, "doc_num": 5, "status": "COMPLETED"}
    assert resultado["facturas"][1] == {"chunk_index": 1, "doc_entry": 101, "doc_num": 5, "status": "COMPLETED"}


async def test_un_chunk_falla_sigue_con_los_demas(session, monkeypatch):
    """I2: un chunk fallido no bloquea a los demás."""
    await _armar_woo_order(session)
    _mock_ok(monkeypatch)

    async def _create_sap_invoice_mixto(s, factura, wo):
        if factura.chunk_index == 0:
            factura.status = "FAILED"
            factura.attempts += 1
            raise ValueError("SAP rechazó el chunk 0")
        factura.status = "COMPLETED"
        return SimpleNamespace(chunk_index=factura.chunk_index, doc_entry=999, doc_num=7, status="COMPLETED")

    monkeypatch.setattr(orchestrator.billing, "create_sap_invoice", _create_sap_invoice_mixto)

    resultado = await orchestrator.sync_order_to_sap(session, 1001)

    assert resultado["error"] is None
    assert resultado["facturas"][0]["status"] == "FAILED"
    assert "SAP rechazó" in resultado["facturas"][0]["error"]
    assert resultado["facturas"][1]["status"] == "COMPLETED"
    assert resultado["facturas"][1]["doc_entry"] == 999


# ── procesar_pedidos_pendientes (automático, Beat) ──────────────────────

async def test_batch_procesa_pending_y_failed_pero_no_completed(session, monkeypatch):
    _mock_ok(monkeypatch)
    pendiente = await _armar_woo_order(session, code=1, tax_id="11111111-1", status="PENDING")
    fallido = await _armar_woo_order(session, code=2, tax_id="22222222-2", status="FAILED")
    await _armar_woo_order(session, code=3, tax_id="33333333-3", status="COMPLETED")

    resultado = await orchestrator.procesar_pedidos_pendientes(session)

    codigos_procesados = {r["code"] for r in resultado["detalle"]}
    assert codigos_procesados == {pendiente.code, fallido.code}
    assert resultado["procesados"] == 2


async def test_batch_reintenta_factura_de_pedido_ya_trozado_sin_repetir_billing(session, monkeypatch):
    """
    Un WooOrder ya COMPLETED (troceo ok) con un SAPBilling todavía FAILED
    -> se reintenta create_sap_invoice directo, sin volver a resolver
    cliente ni trocear de nuevo.
    """
    orden = await _armar_woo_order(session, status="COMPLETED")
    factura = await _armar_sap_billing(session, orden, status="FAILED")

    llamado_resolve = []
    async def _resolve_customer(s, tax_id, datos):
        llamado_resolve.append(True)
    monkeypatch.setattr(orchestrator.customers, "resolve_customer", _resolve_customer)

    async def _create_sap_invoice(s, f, wo):
        f.status = "COMPLETED"
        return SimpleNamespace(chunk_index=f.chunk_index, doc_entry=500, doc_num=9, status="COMPLETED")
    monkeypatch.setattr(orchestrator.billing, "create_sap_invoice", _create_sap_invoice)

    resultado = await orchestrator.procesar_pedidos_pendientes(session)

    assert llamado_resolve == []
    assert resultado["procesados"] == 1
    assert resultado["detalle"][0]["facturas"][0]["doc_entry"] == 500
    assert factura.status == "COMPLETED"


async def test_batch_no_reprocesa_sap_billing_de_pedido_que_ya_se_proceso_arriba(session, monkeypatch):
    """Si el WooOrder está PENDING/FAILED, se procesa completo en el primer
    grupo — el segundo grupo (facturas sueltas) no debe tocarlo de nuevo."""
    orden = await _armar_woo_order(session, status="FAILED")
    await _armar_sap_billing(session, orden, status="FAILED")

    llamados_invoice_directo = []
    _mock_ok(monkeypatch)
    async def _create_sap_invoice(s, f, wo):
        llamados_invoice_directo.append(f)
        f.status = "COMPLETED"
        return SimpleNamespace(chunk_index=f.chunk_index, doc_entry=1, doc_num=1, status="COMPLETED")
    monkeypatch.setattr(orchestrator.billing, "create_sap_invoice", _create_sap_invoice)

    resultado = await orchestrator.procesar_pedidos_pendientes(session)

    assert resultado["procesados"] == 1  # una sola entrada, no duplicada


# ── procesar_facturas_pendientes (Chain B, automático, Beat) ────────────

async def test_batch_factura_pending_corre_fetch_pdf_y_email(session, monkeypatch):
    await _armar_sap_invoice(session, status="PENDING")

    async def _fetch_pdf(s, f):
        f.status = "COMPLETED"
        await s.commit()
    async def _prepare_email(s, f):
        email = Email(sap_invoice_id=f.id, event_type=EmailEventType.CUSTOMER_INVOICE.value, status="PENDING")
        s.add(email)
        await s.commit()
        return email
    async def _send_email(s, e):
        e.status = "COMPLETED"
        await s.commit()

    monkeypatch.setattr(orchestrator.documents, "fetch_pdf", _fetch_pdf)
    monkeypatch.setattr(orchestrator.notifications, "prepare_email", _prepare_email)
    monkeypatch.setattr(orchestrator.notifications, "send_email", _send_email)

    resultado = await orchestrator.procesar_facturas_pendientes(session)

    assert resultado["procesados"] == 1
    assert resultado["exitosos"] == 1
    assert resultado["detalle"][0]["status"] == "COMPLETED"


async def test_batch_factura_con_pdf_ya_completado_solo_reintenta_email(session, monkeypatch):
    """Una factura con PDF listo pero email todavía sin mandar no debe
    volver a llamar fetch_pdf (no tiene guard propio, se evitaría por status)."""
    await _armar_sap_invoice(session, status="COMPLETED")

    llamado_fetch = []
    async def _fetch_pdf(s, f):
        llamado_fetch.append(True)
    async def _prepare_email(s, f):
        email = Email(sap_invoice_id=f.id, event_type=EmailEventType.CUSTOMER_INVOICE.value, status="PENDING")
        s.add(email)
        await s.commit()
        return email
    async def _send_email(s, e):
        e.status = "COMPLETED"
        await s.commit()

    monkeypatch.setattr(orchestrator.documents, "fetch_pdf", _fetch_pdf)
    monkeypatch.setattr(orchestrator.notifications, "prepare_email", _prepare_email)
    monkeypatch.setattr(orchestrator.notifications, "send_email", _send_email)

    resultado = await orchestrator.procesar_facturas_pendientes(session)

    assert llamado_fetch == []
    assert resultado["procesados"] == 1
    assert resultado["exitosos"] == 1


async def test_batch_factura_con_pdf_y_email_ya_completados_no_se_toca(session, monkeypatch):
    factura = await _armar_sap_invoice(session, status="COMPLETED")
    email = Email(
        sap_invoice_id=factura.id, event_type=EmailEventType.CUSTOMER_INVOICE.value, status="COMPLETED",
    )
    session.add(email)
    await session.commit()

    monkeypatch.setattr(
        orchestrator.documents, "fetch_pdf",
        lambda s, f: (_ for _ in ()).throw(AssertionError("no debería llamar fetch_pdf")),
    )
    monkeypatch.setattr(
        orchestrator.notifications, "send_email",
        lambda s, e: (_ for _ in ()).throw(AssertionError("no debería reenviar un email ya completado")),
    )

    resultado = await orchestrator.procesar_facturas_pendientes(session)

    assert resultado["procesados"] == 0


async def test_batch_fetch_pdf_falla_no_intenta_email_y_no_bloquea_las_demas(session, monkeypatch):
    """I2: una factura fallida no bloquea a las demás."""
    await _armar_sap_invoice(session, doc_entry=1, status="PENDING")
    await _armar_sap_invoice(session, doc_entry=2, status="PENDING")

    llamado_email = []
    async def _fetch_pdf(s, f):
        if f.doc_entry == 1:
            f.status = "FAILED"
            await s.commit()
            raise ValueError("Facele caído")
        f.status = "COMPLETED"
        await s.commit()
    async def _prepare_email(s, f):
        llamado_email.append(f.doc_entry)
        email = Email(sap_invoice_id=f.id, event_type=EmailEventType.CUSTOMER_INVOICE.value, status="PENDING")
        s.add(email)
        await s.commit()
        return email
    async def _send_email(s, e):
        e.status = "COMPLETED"
        await s.commit()

    monkeypatch.setattr(orchestrator.documents, "fetch_pdf", _fetch_pdf)
    monkeypatch.setattr(orchestrator.notifications, "prepare_email", _prepare_email)
    monkeypatch.setattr(orchestrator.notifications, "send_email", _send_email)

    resultado = await orchestrator.procesar_facturas_pendientes(session)

    assert llamado_email == [2]  # nunca se llamó para la que falló fetch_pdf
    assert resultado["procesados"] == 2
    assert resultado["exitosos"] == 1
