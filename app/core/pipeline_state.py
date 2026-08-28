"""
Interruptor on/off del procesamiento automático (Beat) — Redis, no .env, para
poder pausar/reanudar sin redeploy. Por defecto APAGADO: un deploy nuevo (o
un Redis recién vaciado) no debe empezar a procesar pedidos reales solo.

Fail-safe DELIBERADAMENTE al revés que app.tasks.locks.pipeline_lock: el
lock falla ABIERTO (corre sin lock) si Redis cae, para no frenar el
pipeline por un problema de infraestructura ajeno. Este flag falla CERRADO
(apagado) — un Redis caído nunca debe traducirse en "procesar a ciegas".
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_REDIS_KEY = "pipeline:enabled"


def _get_redis():
    import redis
    return redis.Redis.from_url(
        settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
    )


def is_enabled() -> bool:
    try:
        return _get_redis().get(_REDIS_KEY) == "1"
    except Exception as exc:
        logger.warning("pipeline_state.is_enabled: Redis no disponible (%s) — asumiendo APAGADO", exc)
        return False


def enable() -> None:
    _get_redis().set(_REDIS_KEY, "1")


def disable() -> None:
    _get_redis().set(_REDIS_KEY, "0")
