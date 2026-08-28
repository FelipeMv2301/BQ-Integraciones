"""
Cliente SMTP directo para alertas internas de error (notify_failure,
BQI-53). A propósito no pasa por Brevo — Brevo es el canal de comunicación
con clientes (CUSTOMER_INVOICE, con template y BCC de facturación); esto es
solo detección interna del equipo, sin relación con esos flujos.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def enviar_correo(destinatarios: list[str], asunto: str, html: str) -> None:
    """
    Envía un correo HTML vía SMTP con STARTTLS. Lanza excepción si falla —
    el caller (notify_failure) decide qué hacer con el error, mismo
    criterio que app.services.brevo.client.enviar_correo.
    """
    mensaje = MIMEMultipart("alternative")
    mensaje["Subject"] = asunto
    mensaje["From"] = settings.EMAIL_SENDER
    mensaje["To"] = ", ".join(destinatarios)
    mensaje.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.EMAIL_SMTP_HOST, settings.EMAIL_SMTP_PORT, timeout=30) as servidor:
        servidor.starttls()
        servidor.login(settings.EMAIL_SENDER, settings.EMAIL_PASSWORD)
        servidor.sendmail(settings.EMAIL_SENDER, destinatarios, mensaje.as_string())
