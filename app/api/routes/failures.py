"""GET /failures, POST /retry/{tabla}/{entity_id} (BQI-61)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.models.email import Email
from app.models.failure import Failure
from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder
from app.pipelines import (
    billing,
    customers,
    documents,
    failure_tracking,
    notifications,
    orchestrator,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/failures")
async def listar_failures(session: AsyncSession = Depends(get_session)) -> list[dict]:
    filas = (
        await session.execute(select(Failure).order_by(Failure.occurred_at.desc()))
    ).scalars().all()
    return [
        {
            "id": f.id, "entity_type": f.entity_type, "entity_id": f.entity_id,
            "stage": f.stage, "error_message": f.error_message,
            "attempts": f.attempts, "occurred_at": f.occurred_at.isoformat(),
            "notified": f.notified,
        }
        for f in filas
    ]


async def _reintentar_woo_order(session: AsyncSession, entidad: WooOrder) -> None:
    """
    Reintenta el ciclo completo (resolve_customer + prepare_billing), no
    solo prepare_billing -- un WooOrder puede haber escalado a EXHAUSTED
    justo por fallar en resolve_customer (bug real, auditoría 2026-09-02:
    antes /retry solo corría prepare_billing, dejando el cliente sin
    resolver aunque el pedido terminara marcado COMPLETED igual, con el
    problema real reapareciendo recién en create_sap_invoice sin traza al
    origen). resolve_customer() es naturalmente idempotente (siempre
    re-consulta SAP antes de decidir POST/PATCH), repetirlo acá no duplica
    nada. orchestrator._procesar_pedido ya escala a EXHAUSTED por su
    cuenta en cada fase (no relanza), por eso este helper no lanza
    tampoco -- el bloque _ESCALAMIENTO del endpoint no aplica a esta tabla.
    """
    await orchestrator._procesar_pedido(session, entidad)


async def _reintentar_sap_customer(session: AsyncSession, entidad: SAPCustomer) -> None:
    woo_order = (
        await session.execute(
            select(WooOrder)
            .where(WooOrder.customer_tax_id == entidad.tax_id)
            .order_by(WooOrder.id.desc())
        )
    ).scalars().first()
    if woo_order is None:
        raise HTTPException(422, f"No hay un WooOrder con tax_id={entidad.tax_id!r} para reconstruir sus datos")
    datos = await customers.construir_datos_cliente(session, woo_order)
    await customers.resolve_customer(session, entidad.tax_id, datos)


async def _reintentar_sap_billing(session: AsyncSession, entidad: SAPBilling) -> None:
    woo_order = await session.get(WooOrder, entidad.woo_order_id)
    if woo_order is None:
        raise HTTPException(422, f"WooOrder {entidad.woo_order_id} asociado no existe")
    await billing.create_sap_invoice(session, entidad, woo_order)


async def _reintentar_sap_invoice(session: AsyncSession, entidad: SAPInvoice) -> None:
    await documents.fetch_pdf(session, entidad)


async def _reintentar_email(session: AsyncSession, entidad: Email) -> None:
    await notifications.send_email(session, entidad)


_TABLAS = {
    "woo_orders": (WooOrder, _reintentar_woo_order),
    "sap_customers": (SAPCustomer, _reintentar_sap_customer),
    "sap_billings": (SAPBilling, _reintentar_sap_billing),
    "sap_invoices": (SAPInvoice, _reintentar_sap_invoice),
    "emails": (Email, _reintentar_email),
}

# (entity_type para Failure, stage, setting de intentos máximos) por tabla —
# usado para escalar a EXHAUSTED si el reintento manual también agota (I6).
_ESCALAMIENTO = {
    "woo_orders": ("WooOrder", "prepare_billing", lambda: settings.SAP_BILLING_MAX_ATTEMPTS),
    "sap_customers": ("SAPCustomer", "resolve_customer", lambda: settings.RESOLVE_CUSTOMER_MAX_ATTEMPTS),
    "sap_billings": ("SAPBilling", "create_sap_invoice", lambda: settings.SAP_BILLING_MAX_ATTEMPTS),
    "sap_invoices": ("SAPInvoice", "fetch_pdf", lambda: settings.FACELE_MAX_ATTEMPTS),
    "emails": ("Email", "send_email", lambda: settings.EMAIL_MAX_ATTEMPTS),
}


@router.post("/retry/{tabla}/{entity_id}")
async def reintentar(tabla: str, entity_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    if tabla not in _TABLAS:
        raise HTTPException(404, f"Tabla desconocida: {tabla!r}. Opciones: {list(_TABLAS)}")

    modelo, funcion_retry = _TABLAS[tabla]
    entidad = await session.get(modelo, entity_id)
    if entidad is None:
        raise HTTPException(404, f"{tabla}/{entity_id} no encontrado")
    if entidad.status == "COMPLETED":
        raise HTTPException(409, f"{tabla}/{entity_id} ya está COMPLETED, no se reintenta")

    try:
        await funcion_retry(session, entidad)
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("retry %s/%s: %s", tabla, entity_id, exc)
        entity_type, stage, max_attempts_fn = _ESCALAMIENTO[tabla]
        await failure_tracking.escalar_si_agotado(session, entidad, entity_type, stage, max_attempts_fn())

    return {"tabla": tabla, "id": entity_id, "status": entidad.status, "status_message": entidad.status_message}
