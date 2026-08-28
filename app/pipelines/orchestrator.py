"""
Orquestador de pedidos hasta SAP y de facturas hasta el correo. Dos chains:

- Chain A (pedido -> SAP): sync_order_to_sap(session, code) manual (endpoint
  /pipeline/sync-order), procesar_pedidos_pendientes(session) automático
  (Beat, task_poll_woo_orders).
- Chain B (folio -> PDF -> email): procesar_facturas_pendientes(session),
  automático (Beat, task_poll_sap_invoices).

Ambas componen pipelines ya probados sueltos. Reintentan FAILED además de
PENDING (I6) — al agotar el límite de intentos, escalan a EXHAUSTED vía
failure_tracking (dejan de reintentarse solas, quedan para /retry manual).
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.email import Email
from app.models.enums import EmailEventType
from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.pipelines import billing, customers, documents, failure_tracking, notifications
from app.pipelines.woo_orders import _pedido_a_woo_order
from app.services.woocommerce import orders as woo_orders_api

logger = logging.getLogger(__name__)


async def _obtener_o_crear_woo_order(session: AsyncSession, code: int) -> WooOrder:
    woo_order = (
        await session.execute(select(WooOrder).where(WooOrder.code == code))
    ).scalar_one_or_none()
    if woo_order:
        return woo_order

    pedido = woo_orders_api.obtener_pedido(code)
    if pedido is None:
        raise ValueError(f"Pedido {code} no existe en WooCommerce")

    woo_order = _pedido_a_woo_order(pedido)
    session.add(woo_order)
    await session.commit()
    return woo_order


async def _escalar_resolve_customer(session: AsyncSession, woo_order: WooOrder, exc: Exception) -> None:
    """
    resolve_customer puede fallar ANTES de crear un SAPCustomer (RUT
    inválido — ni siquiera llega a tocar la tabla) o DESPUÉS (SAP rechaza
    con el cliente ya creado/actualizándose). Si ya existe la fila, se
    escala esa; si no, el único dato real del error es el WooOrder mismo.
    """
    cliente = (
        await session.execute(select(SAPCustomer).where(SAPCustomer.tax_id == woo_order.customer_tax_id))
    ).scalar_one_or_none()
    if cliente is not None:
        await failure_tracking.escalar_si_agotado(
            session, cliente, "SAPCustomer", "resolve_customer", settings.RESOLVE_CUSTOMER_MAX_ATTEMPTS,
        )
        return

    woo_order.attempts += 1
    woo_order.status_message = f"resolve_customer: {exc}"
    await session.commit()
    await failure_tracking.escalar_si_agotado(
        session, woo_order, "WooOrder", "resolve_customer", settings.SAP_BILLING_MAX_ATTEMPTS,
    )


async def _crear_factura_chunk(
    session: AsyncSession, factura: SAPBilling, woo_order: WooOrder, resultado_facturas: list,
) -> None:
    try:
        factura_creada = await billing.create_sap_invoice(session, factura, woo_order)
        resultado_facturas.append({
            "chunk_index": factura_creada.chunk_index,
            "doc_entry": factura_creada.doc_entry,
            "doc_num": factura_creada.doc_num,
            "status": factura_creada.status,
        })
    except Exception as exc:
        await failure_tracking.escalar_si_agotado(
            session, factura, "SAPBilling", "create_sap_invoice", settings.SAP_BILLING_MAX_ATTEMPTS,
        )
        resultado_facturas.append({
            "chunk_index": factura.chunk_index, "status": factura.status, "error": str(exc),
        })


async def _procesar_pedido(session: AsyncSession, woo_order: WooOrder) -> dict:
    """
    Lleva un WooOrder ya existente hasta SAP: resolve_customer ->
    prepare_billing -> create_sap_invoice por cada chunk. No aborta en la
    primera falla de un chunk (I2) — sí se detiene entero si falla una
    fase previa (sin cliente resuelto no tiene sentido facturar).
    """
    resultado: dict = {"code": woo_order.code, "cliente": None, "facturas": [], "error": None}

    try:
        datos_cliente = await customers.construir_datos_cliente(session, woo_order)
        cliente = await customers.resolve_customer(session, woo_order.customer_tax_id, datos_cliente)
        resultado["cliente"] = cliente.code
    except Exception as exc:
        resultado["error"] = f"resolve_customer: {exc}"
        await _escalar_resolve_customer(session, woo_order, exc)
        return resultado

    try:
        facturaciones = await billing.prepare_billing(session, woo_order)
    except Exception as exc:
        resultado["error"] = f"prepare_billing: {exc}"
        await failure_tracking.escalar_si_agotado(
            session, woo_order, "WooOrder", "prepare_billing", settings.SAP_BILLING_MAX_ATTEMPTS,
        )
        return resultado

    for factura in facturaciones:
        await _crear_factura_chunk(session, factura, woo_order, resultado["facturas"])

    return resultado


async def sync_order_to_sap(session: AsyncSession, code: int) -> dict:
    """Endpoint manual (/pipeline/sync-order/{code}) — UN pedido puntual."""
    try:
        woo_order = await _obtener_o_crear_woo_order(session, code)
    except Exception as exc:
        return {"code": code, "cliente": None, "facturas": [], "error": f"Ingesta del pedido: {exc}"}
    return await _procesar_pedido(session, woo_order)


async def procesar_pedidos_pendientes(session: AsyncSession) -> dict:
    """
    Chain A automática (Beat, task_poll_woo_orders). Dos grupos, porque un
    pedido puede quedar "trozado" (WooOrder.status=COMPLETED) con algún
    chunk de factura todavía sin crear en SAP — ese caso no se ve mirando
    solo WooOrder.status:

    1. WooOrder sin trocear o con troceo fallido (PENDING/FAILED) -> ciclo
       completo (resolve_customer + prepare_billing + create_sap_invoice).
    2. SAPBilling ya troceado pero sin factura creada (PENDING/FAILED),
       cuyo WooOrder padre YA está COMPLETED -> solo create_sap_invoice.

    Reintenta FAILED además de PENDING (I6) — al agotar intentos, escala a
    EXHAUSTED (failure_tracking) y deja de reintentarse solo.
    """
    resultados = []

    pedidos_sin_trocear = (
        await session.execute(select(WooOrder).where(WooOrder.status.in_(["PENDING", "FAILED"])))
    ).scalars().all()
    for woo_order in pedidos_sin_trocear:
        resultados.append(await _procesar_pedido(session, woo_order))

    facturas_pendientes = (
        await session.execute(select(SAPBilling).where(SAPBilling.status.in_(["PENDING", "FAILED"])))
    ).scalars().all()
    for factura in facturas_pendientes:
        woo_order = await session.get(WooOrder, factura.woo_order_id)
        if woo_order is None or woo_order.status != "COMPLETED":
            continue  # el WooOrder padre se procesa arriba, no acá dos veces
        facturas_resultado: list = []
        await _crear_factura_chunk(session, factura, woo_order, facturas_resultado)
        resultados.append({
            "code": woo_order.code, "cliente": None, "facturas": facturas_resultado, "error": None,
        })

    exitosos = sum(
        1 for r in resultados
        if r["error"] is None and all(f.get("status") == "COMPLETED" for f in r["facturas"])
    )
    logger.info("procesar_pedidos_pendientes: %d procesados, %d exitosos", len(resultados), exitosos)
    return {"procesados": len(resultados), "exitosos": exitosos, "detalle": resultados}


async def _procesar_factura(session: AsyncSession, factura: SAPInvoice) -> dict:
    """
    Lleva una SAPInvoice (ya con folio) hasta el correo: fetch_pdf ->
    prepare_email -> send_email. Salta fetch_pdf si ya está COMPLETED (no
    tiene guard propio, a diferencia de create_sap_invoice/send_email).
    """
    resultado: dict = {"sap_invoice_id": factura.id, "status": None, "error": None}

    if factura.status in ("PENDING", "FAILED"):
        try:
            await documents.fetch_pdf(session, factura)
        except Exception as exc:
            resultado["error"] = f"fetch_pdf: {exc}"
            await failure_tracking.escalar_si_agotado(
                session, factura, "SAPInvoice", "fetch_pdf", settings.FACELE_MAX_ATTEMPTS,
            )
            resultado["status"] = factura.status
            return resultado

    try:
        email = await notifications.prepare_email(session, factura)
        if email.status in ("PENDING", "FAILED"):
            await notifications.send_email(session, email)
        resultado["status"] = email.status
    except Exception as exc:
        resultado["error"] = f"email: {exc}"
        email_row = (
            await session.execute(
                select(Email).where(
                    Email.sap_invoice_id == factura.id,
                    Email.event_type == EmailEventType.CUSTOMER_INVOICE.value,
                )
            )
        ).scalar_one_or_none()
        if email_row is not None:
            await failure_tracking.escalar_si_agotado(
                session, email_row, "Email", "send_email", settings.EMAIL_MAX_ATTEMPTS,
            )

    return resultado


async def procesar_facturas_pendientes(session: AsyncSession) -> dict:
    """
    Chain B automática (Beat, task_poll_sap_invoices). Dos grupos, mismo
    motivo que Chain A: una SAPInvoice puede tener el PDF listo
    (status=COMPLETED) pero el email todavía sin mandar — eso no se ve
    mirando solo SAPInvoice.status:

    1. Sin PDF o con PDF fallido (PENDING/FAILED) -> fetch_pdf + email.
    2. Con PDF ya COMPLETED pero sin email COMPLETED -> solo email.
    """
    resultados = []

    sin_pdf = (
        await session.execute(select(SAPInvoice).where(SAPInvoice.status.in_(["PENDING", "FAILED"])))
    ).scalars().all()
    for factura in sin_pdf:
        resultados.append(await _procesar_factura(session, factura))

    con_pdf_ids = (
        await session.execute(select(SAPInvoice.id).where(SAPInvoice.status == "COMPLETED"))
    ).scalars().all()
    if con_pdf_ids:
        emails_completados_ids = set(
            (await session.execute(
                select(Email.sap_invoice_id).where(
                    Email.sap_invoice_id.in_(con_pdf_ids),
                    Email.event_type == EmailEventType.CUSTOMER_INVOICE.value,
                    Email.status == "COMPLETED",
                )
            )).scalars().all()
        )
        ids_pendientes = [i for i in con_pdf_ids if i not in emails_completados_ids]
        if ids_pendientes:
            facturas_pendientes_email = (
                await session.execute(select(SAPInvoice).where(SAPInvoice.id.in_(ids_pendientes)))
            ).scalars().all()
            for factura in facturas_pendientes_email:
                resultados.append(await _procesar_factura(session, factura))

    exitosos = sum(1 for r in resultados if r["error"] is None and r["status"] == "COMPLETED")
    logger.info("procesar_facturas_pendientes: %d procesados, %d exitosos", len(resultados), exitosos)
    return {"procesados": len(resultados), "exitosos": exitosos, "detalle": resultados}
