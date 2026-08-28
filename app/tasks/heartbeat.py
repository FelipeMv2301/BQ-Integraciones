"""
Heartbeat hacia Healthchecks.io — "dead-man switch" del scheduler. Si una
tarea deja de correr (Beat caído), Healthchecks no recibe el ping esperado
y alerta. No-op si el slug no tiene un check configurado en
HEALTHCHECKS_CHECKS (dev local). Best-effort: nunca rompe el pipeline si
Healthchecks no responde.
"""

import logging
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)

def _ping(slug: str, suffix: str = "") -> None:
    uuid = settings.healthchecks_checks.get(slug)
    if not uuid:
        return

    base = settings.HEALTHCHECKS_BASE_URL.rstrip("/")
    url = f"{base}/{uuid}"
    if suffix:
        url = f"{url}/{suffix}"

    try:
        import requests
        requests.get(url, timeout=settings.HEALTHCHECKS_TIMEOUT)
    except Exception as exc:
        label = f"{slug}/{suffix}" if suffix else slug
        logger.warning("heartbeat(%s): ping a Healthchecks falló (%s)", label, exc)

@contextmanager
def heartbeat(slug: str):
    """Al entrar: ping /start. Al salir sin error: ping de éxito. Si lanza
    excepción: ping /fail y re-lanza (el caller decide qué hacer)."""
    _ping(slug, "start")
    try:
        yield
    except Exception:
        _ping(slug, "fail")
        raise
    else:
        _ping(slug)