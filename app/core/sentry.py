"""
Inicialización de Sentry — rastreo de errores (web + worker). Puerto directo
de Stock-Service/app/core/sentry.py (ya genérico, sin nada propio de ese
proyecto que adaptar).

Sentry captura excepciones no manejadas con stacktrace y contexto, en los
dos procesos del servicio (FastAPI y Celery). Complementa al heartbeat de
Healthchecks.io (app/tasks/heartbeat.py):
  - Sentry        → "algo se ejecutó y reventó" (error con traza).
  - Healthchecks  → "algo dejó de ejecutarse" (el scheduler no corrió).

sentry-sdk autodetecta y activa las integraciones de FastAPI/Starlette y
Celery cuando esas librerías están presentes, así que basta con llamar a
init_sentry() una vez por proceso.

Privacidad: send_default_pii=False — NO se adjuntan cuerpos de request,
cookies ni headers.
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_initialized = False


def init_sentry(component: str) -> None:
    """
    Inicializa Sentry para el componente dado ("web" o "worker").

    No-op si SENTRY_DSN no está configurado (desarrollo local).
    Idempotente: una segunda llamada no reinicializa.
    Best-effort: si el SDK no está o falla, se registra un warning y el
    servicio continúa — el monitoreo nunca debe impedir el arranque.
    """
    global _initialized
    if _initialized or not settings.SENTRY_DSN:
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            release=settings.SENTRY_RELEASE or None,
            # 0.0 = solo errores (sin performance tracing) — amable con la capa
            # gratuita. Subir si se quiere muestreo de transacciones.
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            # No adjuntar PII a los eventos.
            send_default_pii=False,
            # Verboso solo para verificar la conexión (SENTRY_DEBUG=true).
            # Mantener en False en producción para no ensuciar los logs.
            debug=settings.SENTRY_DEBUG,
        )
        sentry_sdk.set_tag("component", component)
        _initialized = True
        logger.info(
            "Sentry inicializado (component=%s, env=%s)",
            component, settings.ENVIRONMENT,
        )
    except Exception as exc:
        logger.warning("No se pudo inicializar Sentry: %s", exc)
