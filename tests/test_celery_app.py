"""Test de sanidad del beat_schedule — evita un typo silencioso en producción."""

from app.tasks.celery_app import celery_app


def test_beat_schedule_referencia_tareas_existentes():
    nombres = {entrada["task"] for entrada in celery_app.conf.beat_schedule.values()}
    assert "app.tasks.scheduled.task_poll_woo_orders" in nombres
    assert "app.tasks.scheduled.task_poll_sap_invoices" in nombres
    assert "app.tasks.scheduled.task_flush_api_logs" in nombres
