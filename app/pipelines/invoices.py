"""
Pipeline de espera de folio (BQI-40). Para cada SAPBilling ya creado en
SAP (status COMPLETED, doc_entry asignado) que todavía no tiene su
SAPInvoice, consulta a SAP si ya le asignaron folio — "esperar el folio"
es literalmente este polling (R3): no hay evento que lo avise, solo se
sabe consultando.
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.services.sap import client

logger = logging.getLogger(__name__)


def _consultar_folio(doc_entry: int) -> dict | None:
    """GET puntual a Invoices(doc_entry). None si SAP aún no le asignó folio."""
    respuesta = client.solicitar(
        "GET", f"Invoices({doc_entry})",
        params={"$select": "DocEntry,DocNum,FolioNumber,FolioPrefixString"},
    )
    respuesta.raise_for_status()
    datos = respuesta.json()
    return datos if datos.get("FolioNumber") else None


async def _buscar_cliente(session: AsyncSession, tax_id: str | None) -> SAPCustomer | None:
    if not tax_id:
        return None
    return (
        await session.execute(select(SAPCustomer).where(SAPCustomer.tax_id == tax_id))
    ).scalar_one_or_none()


async def poll_sap_invoices(session: AsyncSession) -> dict:
    """
    Recorre los SAPBilling completados sin SAPInvoice todavía y consulta
    si SAP ya les asignó folio. Si no, se deja para el próximo ciclo —sin
    marcar ningún error, es el comportamiento normal de "esperar" (a
    diferencia de PermanentError/TransientError de otras etapas, este NO
    es un fallo, es solo "todavía no").
    """
    ya_con_factura = set(
        (await session.execute(select(SAPInvoice.doc_entry))).scalars().all()
    )
    facturaciones = (
        await session.execute(
            select(SAPBilling).where(
                SAPBilling.status == "COMPLETED",
                SAPBilling.doc_entry.is_not(None),
            )
        )
    ).scalars().all()
    pendientes = [f for f in facturaciones if f.doc_entry not in ya_con_factura]

    nuevas = 0
    for factura in pendientes:
        datos = _consultar_folio(factura.doc_entry)
        if datos is None:
            continue

        woo_order = await session.get(WooOrder, factura.woo_order_id)
        cliente = await _buscar_cliente(session, woo_order.customer_tax_id if woo_order else None)

        session.add(SAPInvoice(
            sap_billing_id=factura.id,
            doc_entry=datos["DocEntry"],
            doc_num=datos["DocNum"],
            folio=datos["FolioNumber"],
            folio_prefix=datos.get("FolioPrefixString"),
            doc_type_code=factura.doc_type_code,
            customer_email=cliente.email if cliente else None,
            contact_email=cliente.contact_email if cliente else None,
        ))
        nuevas += 1

    await session.commit()
    logger.info("poll_sap_invoices: %d con folio nuevo (de %d pendientes)", nuevas, len(pendientes))
    return {"pendientes": len(pendientes), "nuevas": nuevas}
