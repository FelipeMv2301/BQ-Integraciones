"""Pipeline de persistencia de ApiLog (BQI-63). Puerto de
Stock-Service/app/pipelines/cleanup.py::flush_api_logs — sin la limpieza
por retención de ese proyecto, que no forma parte de este ticket."""

import logging
from datetime import datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.api_log import drain_api_logs
from app.models.api_log import ApiLog

logger = logging.getLogger(__name__)


async def flush_api_logs(session: AsyncSession) -> dict:
    """
    Drena la cola Redis de ApiLog y persiste los registros en PostgreSQL.
    Llamada por task_flush_api_logs cada 5 minutos.
    """
    entries = drain_api_logs(max_items=1000)
    if not entries:
        return {"flushed": 0}

    for entry in entries:
        created_at = entry.pop("created_at", None)
        log = ApiLog(**entry)
        if created_at:
            log.created_at = datetime.fromisoformat(created_at)
        session.add(log)

    await session.commit()
    logger.info("ApiLog: %d registros persistidos desde Redis", len(entries))
    return {"flushed": len(entries)}
