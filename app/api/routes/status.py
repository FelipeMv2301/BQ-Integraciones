"""GET /status (BQI-60) — conteo de filas por status en cada tabla de trabajo."""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.models.email import Email
from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.sap_invoice import SAPInvoice
from app.models.woo_order import WooOrder

router = APIRouter()

_TABLAS = {
    "woo_orders": WooOrder,
    "sap_customers": SAPCustomer,
    "sap_billings": SAPBilling,
    "sap_invoices": SAPInvoice,
    "emails": Email,
}


async def _conteo_por_estado(session: AsyncSession, modelo) -> dict:
    resultado = await session.execute(select(modelo.status, func.count()).group_by(modelo.status))
    return {estado: total for estado, total in resultado.all()}


@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)) -> dict:
    return {nombre: await _conteo_por_estado(session, modelo) for nombre, modelo in _TABLAS.items()}
