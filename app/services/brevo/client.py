"""
Cliente HTTP de Brevo (BQI-50). Capa delgada: solo hace el POST autenticado.
Decidir si el envío fue exitoso (status + messageId) es responsabilidad del
pipeline (app/pipelines/notifications.py), no de este cliente.
"""

import requests

from app.core.config import settings


def enviar_correo(payload: dict) -> requests.Response:
    """POST smtp/email. No lanza en 4xx/5xx — Brevo devuelve JSON de error
    (campo `message`) en vez de un HTTP error genérico; el pipeline decide
    qué hacer con la respuesta."""
    return requests.post(
        f"{settings.BREVO_URL}smtp/email",
        json=payload,
        headers={"api-key": settings.BREVO_API_KEY, "Content-Type": "application/json"},
        timeout=30,
    )
