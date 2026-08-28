"""
POST /pipeline/sync-order/{code} — sincroniza manualmente UN pedido puntual
hasta SAP, para pruebas dirigidas.

GET /pipeline/status, POST /pipeline/enable, POST /pipeline/disable —
interruptor del procesamiento automático (Beat). Funcionan siempre, esté
prendido o apagado el flag; el sync manual tampoco depende de él.
"""

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import pipeline_state
from app.core.database import get_session
from app.pipelines.orchestrator import sync_order_to_sap

router = APIRouter()


@router.post("/pipeline/sync-order/{code}")
async def sync_order(code: int, session: AsyncSession = Depends(get_session)) -> dict:
    return await sync_order_to_sap(session, code)


@router.get("/pipeline/status")
async def pipeline_status() -> dict:
    return {"enabled": pipeline_state.is_enabled()}


@router.post("/pipeline/enable")
async def pipeline_enable() -> dict:
    pipeline_state.enable()
    return {"enabled": True}


@router.post("/pipeline/disable")
async def pipeline_disable() -> dict:
    pipeline_state.disable()
    return {"enabled": False}
