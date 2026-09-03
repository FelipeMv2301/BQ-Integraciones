"""
Tareas programadas de Celery Beat.

task_poll_woo_orders conecta la ingesta (BQI-31) + Chain A
(app.pipelines.orchestrator::procesar_pedidos_pendientes) — resolve_customer
+ prepare_billing + create_sap_invoice. task_poll_sap_invoices conecta la
espera de folio (BQI-40) + Chain B (procesar_facturas_pendientes) —
fetch_pdf + prepare_email + send_email.
"""

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from celery import shared_task

from app.core import pipeline_state
from app.core.config import settings
from app.tasks.heartbeat import heartbeat
from app.tasks.locks import pipeline_lock

logger = logging.getLogger(__name__)

def _retry_countdown(attempt: int) -> int:
    return min(60 * (2 ** attempt), 600) + random.randint(0, 30)

def _run_async(coro):
    """
    Ejecuta una corrutina async desde Celery (síncrono). Usa asyncio.run
    (no get_event_loop().run_until_complete): crea un loop nuevo, lo cierra
    al terminar — combinado con NullPool en el engine del worker
    (core/database.py, BQI-03), evita "Event loop is closed" al reutilizar
    conexiones asyncpg de un loop cerrado entre tareas.
    """
    return asyncio.run(coro)

@shared_task(bind=True, name="app.tasks.scheduled.task_poll_woo_orders", max_retries=3)
def task_poll_woo_orders(self):
    """Programada cada WOO_POLL_INTERVAL_MINUTES. Ver BQI-31."""
    if not pipeline_state.is_enabled():
        return {"skipped": "disabled"}
    try:
        with pipeline_lock("poll_woo_orders") as acquired:
            if not acquired:
                logger.info("task_poll_woo_orders omitida: otra corrida en curso")
                return {"skipped": "lock"}
            with heartbeat("poll-woo-orders"):
                result = _run_async(_ciclo_woo_orders())
        return result
    except Exception as exc:
        logger.error("task_poll_woo_orders falló: %s", exc)
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))

async def _ciclo_woo_orders() -> dict:
    """
    Ventana fija hacia atrás (date_from/date_to) — mismo patrón que
    Integrify-Consola (recalculada cada corrida, sin checkpoint
    persistido). Sin esto, poll_woo_orders() trae TODA la historia de
    pedidos de la tienda en cada ciclo — bug real encontrado 2026-08-18 al
    probar el batch por primera vez (3031 pedidos "nuevos" de golpe). El
    dedup por `code` que ya tiene poll_woo_orders protege el solape de la
    ventana, no hace falta guardar un timestamp aparte.

    status="processing" -- mismo filtro que aplicaba el path nativo
    (retirado 2026-09-02), solo pedidos ya pagados/en curso, no on-hold ni
    cancelados.
    """
    from app.core.database import AsyncSessionLocal
    from app.pipelines.orchestrator import procesar_pedidos_pendientes
    from app.pipelines.woo_orders import poll_woo_orders

    hoy = datetime.now(UTC).date()
    date_from = (hoy - timedelta(days=settings.WOO_POLL_LOOKBACK_DAYS)).isoformat()
    date_to = (hoy + timedelta(days=1)).isoformat()  # incluye el día de hoy completo

    async with AsyncSessionLocal() as session:
        ingesta = await poll_woo_orders(session, date_from=date_from, date_to=date_to, status="processing")
        procesamiento = await procesar_pedidos_pendientes(session)
        return {"ingesta": ingesta, "procesamiento": procesamiento}

@shared_task(bind=True, name="app.tasks.scheduled.task_poll_sap_invoices", max_retries=3)
def task_poll_sap_invoices(self):
    """Programada cada SAP_INVOICE_POLL_INTERVAL_MINUTES. Ver BQI-40."""
    if not pipeline_state.is_enabled():
        return {"skipped": "disabled"}
    try:
        with pipeline_lock("poll_sap_invoices") as acquired:
            if not acquired:
                logger.info("task_poll_sap_invoices omitida: otra corrida en curso")
                return {"skipped": "lock"}
            with heartbeat("poll-sap-invoices"):
                result = _run_async(_ciclo_sap_invoices())
        return result
    except Exception as exc:
        logger.error("task_poll_sap_invoices falló: %s", exc)
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))

async def _ciclo_sap_invoices() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.pipelines.invoices import poll_sap_invoices
    from app.pipelines.orchestrator import procesar_facturas_pendientes

    async with AsyncSessionLocal() as session:
        folio = await poll_sap_invoices(session)
        procesamiento = await procesar_facturas_pendientes(session)
        return {"folio": folio, "procesamiento": procesamiento}

@shared_task(bind=True, name="app.tasks.scheduled.task_flush_api_logs", max_retries=1, default_retry_delay=60)
def task_flush_api_logs(self):
    """
    Persiste en PostgreSQL los registros de ApiLog encolados en Redis
    (BQI-63). Programada cada 5 minutos.
    """
    from app.core.database import AsyncSessionLocal
    from app.pipelines.cleanup import flush_api_logs

    async def _run():
        async with AsyncSessionLocal() as session:
            return await flush_api_logs(session=session)

    try:
        result = _run_async(_run())
        if result.get("flushed", 0):
            logger.info("task_flush_api_logs: %d registros", result["flushed"])
        return result
    except Exception as exc:
        logger.error("task_flush_api_logs falló: %s", exc)
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))