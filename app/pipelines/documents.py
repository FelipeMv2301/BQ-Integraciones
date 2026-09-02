"""
Pipeline de obtención de PDF (BQI-43). Para una SAPInvoice con folio pero
sin PDF todavía, consulta Facele/Docele y guarda el PDF decodificado.
"""

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import SyncStatus
from app.models.sap_invoice import SAPInvoice
from app.pipelines.errors import marcar_fallido
from app.services.facele import client as facele_client

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """Error de negocio no reintentable (PDF malformado en una respuesta exitosa)."""


class TransientError(Exception):
    """Error transitorio (folio aún no propagado a Docele, red, estado=0) — reintentable."""


async def fetch_pdf(session: AsyncSession, factura: SAPInvoice) -> SAPInvoice:
    """
    Consulta el PDF de una factura/boleta ya con folio. El PDF nunca se
    guarda si Facele/Docele no confirmó éxito explícito (I7) — estado=0 se
    trata como transitorio (puede ser que el folio aún no se propagó a
    Docele, no hay forma de distinguirlo de un error real por la
    respuesta), se reintenta hasta agotar FACELE_MAX_ATTEMPTS.
    """
    try:
        resultado = facele_client.obtener_documento(
            doc_type_code=int(factura.doc_type_code), folio=factura.folio
        )
    except Exception as exc:
        await marcar_fallido(
            session, factura, f"Error consultando Facele/Docele: {exc}", TransientError, cause=exc
        )

    if resultado["estado"] != 1:
        mensaje = f"Facele/Docele: {resultado['descripcion']} (estado={resultado['estado']})"
        await marcar_fallido(session, factura, mensaje, TransientError)

    if not resultado.get("pdf"):
        await marcar_fallido(session, factura, "Facele/Docele: estado=1 pero sin PDF", TransientError)

    try:
        pdf_decodificado = facele_client.decodificar_pdf(resultado["pdf"])
    except ValueError as exc:
        await marcar_fallido(session, factura, str(exc), PermanentError, cause=exc)

    factura.pdf_base64 = pdf_decodificado
    factura.status, factura.status_message = SyncStatus.COMPLETED, None
    factura.attempts += 1
    await session.commit()
    return factura