"""
Configuración de Celery. Worker ejecuta tareas encoladas; Beat las dispara
según horario. task_acks_late=True: si el worker muere a mitad de una tarea,
vuelve a la cola y otro worker la retoma.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.sentry import init_sentry

init_sentry("worker")

celery_app = Celery(
    "bq-integraciones",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.scheduled"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    timezone=settings.TZ,
    enable_utc=True,

    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_max_retries=3,

    beat_schedule={
        "poll-woo-orders": {
            "task": "app.tasks.scheduled.task_poll_woo_orders",
            "schedule": crontab(minute=f"*/{settings.WOO_POLL_INTERVAL_MINUTES}"),
        },
        "poll-sap-invoices": {
            "task": "app.tasks.scheduled.task_poll_sap_invoices",
            "schedule": crontab(minute=f"*/{settings.SAP_INVOICE_POLL_INTERVAL_MINUTES}"),
        },
        "flush-api-logs": {
            "task": "app.tasks.scheduled.task_flush_api_logs",
            "schedule": crontab(minute="*/5"),
        },
    },
)