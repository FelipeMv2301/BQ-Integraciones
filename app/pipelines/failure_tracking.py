"""
Escalamiento a EXHAUSTED + alerta cuando una entidad agota sus reintentos
(I6). Se llama DESPUÉS de que la función de pipeline ya marcó FAILED e
incrementó `attempts` — nunca antes.

Centralizado acá (no dentro de cada pipeline function) a propósito: hay
~10 puntos de falla repartidos en customers.py/billing.py/documents.py/
notifications.py, y el caller (batch automático o /retry) ya tiene la
entidad actualizada en memoria apenas la excepción se propaga — no hace
falta tocar cada uno.
"""

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.failure import Failure
from app.pipelines.notifications import notify_failure

logger = logging.getLogger(__name__)


async def escalar_si_agotado(
    session: AsyncSession, entidad, entity_type: str, stage: str, max_attempts: int,
) -> None:
    """
    Si `entidad.attempts` todavía no llegó a `max_attempts`, no hace nada
    (sigue FAILED, se reintentará el próximo ciclo). Si ya lo alcanzó: sube
    a EXHAUSTED, deja registro en `failures`, y dispara notify_failure —
    a partir de acá hace falta revisión manual (vía /retry), no se
    reintenta solo nunca más.
    """
    if entidad.status == "EXHAUSTED" or entidad.attempts < max_attempts:
        return

    entidad.status = "EXHAUSTED"
    session.add(Failure(
        entity_type=entity_type,
        entity_id=entidad.id,
        stage=stage,
        error_message=entidad.status_message or "Sin mensaje",
        attempts=entidad.attempts,
    ))
    await session.commit()

    kind = f"{entity_type}:{stage}"
    mensaje = (
        f"{entity_type} #{entidad.id} agotó reintentos en {stage} "
        f"({entidad.attempts} intentos): {entidad.status_message}"
    )
    notify_failure(kind, mensaje)
    logger.warning("escalar_si_agotado: %s #%s -> EXHAUSTED (%s)", entity_type, entidad.id, stage)
