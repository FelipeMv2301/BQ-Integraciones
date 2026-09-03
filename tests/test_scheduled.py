"""Tests del guard de pipeline_state en app.tasks.scheduled — sin red, sin Celery real."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.tasks import scheduled


def test_poll_woo_orders_no_hace_nada_si_esta_apagado(monkeypatch):
    monkeypatch.setattr(scheduled.pipeline_state, "is_enabled", lambda: False)
    monkeypatch.setattr(
        scheduled, "pipeline_lock",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debería intentar el lock si está apagado")),
    )

    assert scheduled.task_poll_woo_orders.run() == {"skipped": "disabled"}


def test_poll_sap_invoices_no_hace_nada_si_esta_apagado(monkeypatch):
    monkeypatch.setattr(scheduled.pipeline_state, "is_enabled", lambda: False)
    monkeypatch.setattr(
        scheduled, "pipeline_lock",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debería intentar el lock si está apagado")),
    )

    assert scheduled.task_poll_sap_invoices.run() == {"skipped": "disabled"}


def test_poll_woo_orders_prendido_llama_al_ciclo_real(monkeypatch):
    """Confirma que, prendido, pasa el lock+heartbeat y llega a _ciclo_woo_orders."""
    monkeypatch.setattr(scheduled.pipeline_state, "is_enabled", lambda: True)

    async def _ciclo_fake():
        return {"ingesta": {"nuevos": 0}, "procesamiento": {"procesados": 0}}
    monkeypatch.setattr(scheduled, "_ciclo_woo_orders", _ciclo_fake)

    resultado = scheduled.task_poll_woo_orders.run()

    assert resultado == {"ingesta": {"nuevos": 0}, "procesamiento": {"procesados": 0}}


def test_poll_sap_invoices_prendido_llama_al_ciclo_real(monkeypatch):
    """Confirma que, prendido, pasa el lock+heartbeat y llega a _ciclo_sap_invoices."""
    monkeypatch.setattr(scheduled.pipeline_state, "is_enabled", lambda: True)

    async def _ciclo_fake():
        return {"folio": {"nuevas": 0}, "procesamiento": {"procesados": 0}}
    monkeypatch.setattr(scheduled, "_ciclo_sap_invoices", _ciclo_fake)

    resultado = scheduled.task_poll_sap_invoices.run()

    assert resultado == {"folio": {"nuevas": 0}, "procesamiento": {"procesados": 0}}


def test_ciclo_woo_orders_pasa_date_from_con_la_ventana_configurada(monkeypatch):
    """
    Regresión: sin esto, poll_woo_orders trae TODA la historia de pedidos
    en cada ciclo (bug real encontrado 2026-08-18, 3031 pedidos de golpe).
    """
    capturado = {}

    async def _fake_poll(session, date_from, date_to, status=None):
        capturado["date_from"], capturado["date_to"], capturado["status"] = date_from, date_to, status
        return {"nuevos": 0}

    async def _fake_procesar(session):
        return {"procesados": 0}

    monkeypatch.setattr("app.pipelines.woo_orders.poll_woo_orders", _fake_poll)
    monkeypatch.setattr("app.pipelines.orchestrator.procesar_pedidos_pendientes", _fake_procesar)

    asyncio.run(scheduled._ciclo_woo_orders())

    hoy = datetime.now(UTC).date()
    esperado_desde = (hoy - timedelta(days=settings.WOO_POLL_LOOKBACK_DAYS)).isoformat()
    esperado_hasta = (hoy + timedelta(days=1)).isoformat()
    assert capturado["date_from"] == esperado_desde
    assert capturado["date_to"] == esperado_hasta
    assert capturado["status"] == "processing"
