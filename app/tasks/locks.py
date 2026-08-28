"""
Lock distribuido en Redis para garantizar ejecución única de cada tarea
programada. Sin esto, dos disparos solapados (ciclo de Beat + disparo manual,
o dos workers tomando la misma tarea) podrían procesar el mismo lote dos
veces. SET NX EX: solo un proceso lo adquiere, el resto omite la corrida.

Si Redis no responde (siendo el broker de Celery, no debería pasar), se
procede SIN lock en vez de bloquear el pipeline — se registra un warning.
"""

import logging
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)

_LOCK_PREFIX = "pipeline:lock:"

@contextmanager
def pipeline_lock(name: str, ttl_seconds: int = 900):
    """
    Uso:
        with pipeline_lock("poll_woo_orders") as acquired:
            if not acquired:
                return {"skipped": "lock"}
            ...trabajo...

    ttl_seconds debe ser mayor que la duración máxima esperada de la tarea,
    para no dejar un lock huérfano si el worker muere a mitad de camino.
    """
    key = f"{_LOCK_PREFIX}{name}"
    client = None
    acquired = False

    try:
        import redis
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        acquired = bool(client.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as exc:
        logger.warning("pipeline_lock(%s): Redis no disponible (%s) — corriendo sin lock", name, exc)
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired and client is not None:
            try:
                client.delete(key)
            except Exception as exc:
                logger.warning("pipeline_lock(%s): no se pudo liberar el lock (%s)", name, exc)