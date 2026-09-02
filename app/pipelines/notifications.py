"""
Pipeline de notificaciones por correo (BQI-51/52/53). Factura al cliente vía
Brevo (con PDF adjunto, template) y alerta interna de fallo definitivo vía
SMTP directo (notify_failure, BQI-53) — canales separados a propósito: Brevo
es comunicación con clientes, SMTP es detección interna del equipo, sin
relación entre ambos flujos.
"""

import logging

from email_validator import EmailNotValidError, validate_email
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.email import Email
from app.models.enums import EmailEventType, SyncStatus
from app.models.reference_data import BillDocumentType
from app.models.sap_invoice import SAPInvoice
from app.pipelines.errors import marcar_fallido
from app.services.brevo import client as brevo_client
from app.services.smtp import client as smtp_client

logger = logging.getLogger(__name__)


class TransientError(Exception):
    """Error transitorio (Brevo no disponible, rechazo temporal) — reintentable."""


def _es_email_valido(direccion: str) -> bool:
    try:
        validate_email(direccion, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def _destinatario(direccion: str) -> dict:
    return {"name": direccion, "email": direccion}


async def prepare_email(session: AsyncSession, factura: SAPInvoice) -> Email:
    """
    Construye (o recalcula) la notificación CUSTOMER_INVOICE de una
    SAPInvoice ya con PDF. R5: destinatario = contact_email si existe, si
    no customer_email. BCC = vendedor (seller_email, hoy sin llenar, ver
    BQI-40) + BREVO_INVOICE_BCC.

    Idempotente por sap_invoice_id (mismo patrón que prepare_billing): si
    ya existe la notificación, se actualiza en vez de duplicarse. Si no hay
    ningún destinatario válido, queda SKIPPED (I8) — nunca bloquea el
    pedido ni reintenta indefinidamente.

    Guard: si ya está COMPLETED, no recalcula nada — evita regresar
    silenciosamente a PENDING/SKIPPED una notificación que ya se envió.
    """
    email = (
        await session.execute(
            select(Email).where(
                Email.sap_invoice_id == factura.id,
                Email.event_type == EmailEventType.CUSTOMER_INVOICE.value,
            )
        )
    ).scalar_one_or_none() or Email(
        event_type=EmailEventType.CUSTOMER_INVOICE.value, sap_invoice_id=factura.id
    )

    if email.status == "COMPLETED":
        return email

    destinatario_email = factura.contact_email or factura.customer_email
    to = [_destinatario(destinatario_email)] if destinatario_email else []

    bcc_direcciones = list(settings.invoice_bcc_recipients)
    if factura.seller_email:
        bcc_direcciones.append(factura.seller_email)
    bcc = [_destinatario(d) for d in bcc_direcciones]

    email.to, email.bcc = to, bcc

    direcciones = [d["email"] for d in to + bcc]
    invalidas = [d for d in direcciones if not _es_email_valido(d)]

    if not to or invalidas:
        motivo = "Sin destinatario válido" if not to else f"Direcciones inválidas: {', '.join(invalidas)}"
        email.status, email.status_message = "SKIPPED", motivo
    else:
        email.status, email.status_message = "PENDING", None

    session.add(email)
    await session.commit()
    return email

async def _nombre_tipo_documento(session: AsyncSession, doc_type_code: str | None) -> str:
    if not doc_type_code:
        return ""
    tipo = (
        await session.execute(select(BillDocumentType).where(BillDocumentType.sap_code == doc_type_code))
    ).scalar_one_or_none()
    return tipo.name if tipo else doc_type_code


async def _payload_customer_invoice(session: AsyncSession, email: Email, factura: SAPInvoice) -> dict:
    payload = {
        "to": email.to,
        "templateId": int(settings.BREVO_TEMPLATE_CUSTOMER_INVOICE),
        "params": {
            "FOLIO": factura.folio,
            "DOC_TYPE": await _nombre_tipo_documento(session, factura.doc_type_code),
        },
    }
    if email.bcc:
        payload["bcc"] = email.bcc
    if factura.pdf_base64:
        payload["attachment"] = [{"content": factura.pdf_base64, "name": f"{factura.folio}.pdf"}]
    return payload


async def send_email(session: AsyncSession, email: Email) -> Email:
    """
    Envía la notificación vía Brevo (BQI-52). Solo marca COMPLETED con el
    messageId devuelto por Brevo (I1) — nunca solo por HTTP 200. El máximo
    de intentos (EMAIL_MAX_ATTEMPTS) lo filtra el driver que llama a esta
    función (E6), igual criterio que las demás entidades.

    Guard: si ya está COMPLETED, no vuelve a llamar a Brevo — evita
    reenviar un correo real al cliente ante un reintento.
    """
    if email.status == "COMPLETED":
        return email

    if email.event_type != EmailEventType.CUSTOMER_INVOICE.value:
        raise NotImplementedError(f"send_email: evento {email.event_type!r} aún no soportado")

    factura = await session.get(SAPInvoice, email.sap_invoice_id)
    if factura is None:
        await marcar_fallido(session, email, "SAPInvoice asociada no existe", TransientError)

    payload = await _payload_customer_invoice(session, email, factura)

    if not settings.is_production:
        destino_real = ", ".join(d["email"] for d in email.to) or "sin destinatario"
        payload["to"] = [_destinatario(d) for d in settings.alert_recipients] or [
            _destinatario("felipe.morales@bioquimica.cl")
        ]
        payload.pop("bcc", None)
        payload["subject"] = f"[PRUEBA] Factura {factura.folio} — destino real: {destino_real}"

    try:
        respuesta = brevo_client.enviar_correo(payload)
    except Exception as exc:
        await marcar_fallido(session, email, f"Error de red con Brevo: {exc}", TransientError, cause=exc)

    datos = respuesta.json() if respuesta.content else {}

    if respuesta.ok and datos.get("messageId"):
        email.brevo_message_id = datos["messageId"]
        email.status, email.status_message = SyncStatus.COMPLETED, None
        email.attempts += 1
        await session.commit()
        return email

    mensaje = datos.get("message") or f"HTTP {respuesta.status_code}: {respuesta.text[:500]}"
    await marcar_fallido(session, email, f"Brevo: {mensaje}", TransientError)

_FAILURE_LOCK_PREFIX = "notify:failure:"
_FAILURE_WINDOW_SECONDS = 3600


def notify_failure(kind: str, mensaje: str) -> bool:
    """
    Alerta interna de fallo definitivo (BQI-53). Un mismo `kind` de fallo
    dispara UN solo correo por ventana de 1h, sin importar cuántas veces
    ocurra — evita que un problema recurrente (SAP caído, Facele caído)
    inunde la bandeja con correos idénticos. Devuelve True si se envió.
    """
    key = f"{_FAILURE_LOCK_PREFIX}{kind}"
    conteo = 1
    ya_notificado = False

    try:
        import redis
        r = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
        )
        conteo = r.incr(key)
        if conteo == 1:
            r.expire(key, _FAILURE_WINDOW_SECONDS)
        else:
            ya_notificado = True
    except Exception as exc:
        logger.warning("notify_failure(%s): Redis no disponible (%s) — se envía igual", kind, exc)

    if ya_notificado:
        return False

    destinatarios = settings.alert_recipients
    if not destinatarios:
        logger.error("notify_failure(%s): ALERT_EMAILS vacío — no hay a quién avisar", kind)
        return False

    asunto = f"[BQ-Integraciones] Fallo definitivo: {kind}"
    html = f"<p>{mensaje}</p><p>Ocurrencia #{conteo} de este tipo en la última hora.</p>"

    try:
        smtp_client.enviar_correo(destinatarios, asunto, html)
    except Exception as exc:
        logger.error("notify_failure(%s): SMTP rechazó la alerta - %s", kind, exc)
        return False
    return True
