"""
Helper compartido para el patrón repetido al fallar una llamada externa:
marcar la entidad FAILED, subir attempts, comitear, y propagar el error --
en ese orden exacto, siempre.

Antes de esto el patrón vivía copy-pasteado ~13-16 veces en
customers.py/billing.py/documents.py/notifications.py. El riesgo real: una
llamada a SAP/Stock-Service/Brevo/Facele sin este wrapper podía fallar
ANTES de que el código llegara a la línea que sube `attempts`, dejando la
entidad reintentándose en silencio para siempre -- escalar_si_agotado()
(failure_tracking.py) solo escala cuando `attempts >= max_attempts`, así
que si `attempts` nunca sube, nunca escala, nunca aparece en `/failures`
(hallazgo real, auditoría 2026-09-02).

Vive en su propio módulo, no en failure_tracking.py: failure_tracking.py
importa notifications.py (notify_failure), y notifications.py necesita
este helper también -- ponerlo en failure_tracking.py crearía un import
circular.
"""

from typing import NoReturn

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import SyncStatus


async def marcar_fallido(
    session: AsyncSession,
    entidad,
    mensaje: str,
    excepcion: type[Exception],
    cause: Exception | None = None,
) -> NoReturn:
    """
    Marca `entidad` FAILED, sube `attempts`, comitea, y relanza
    `excepcion(mensaje)` (encadenado a `cause` si se pasa). El commit
    ocurre SIEMPRE antes de que la excepción se propague -- así
    escalar_si_agotado(), llamado por el caller tras capturar la
    excepción, ve el `attempts` ya actualizado.
    """
    entidad.status, entidad.status_message = SyncStatus.FAILED, mensaje
    entidad.attempts += 1
    await session.commit()
    if cause is not None:
        raise excepcion(mensaje) from cause
    raise excepcion(mensaje)
