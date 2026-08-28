"""
Cliente HTTP para Stock-Service — catálogo de productos (SKU -> bodega,
precio, stock). Solo lectura, nunca escribimos ahí. Cachea cada respuesta
en Redis (TTL corto) para no golpear la API por cada línea de cada pedido
— no mantenemos un espejo propio del catálogo (plan.md, decisión D2).
"""

import json
import logging

import redis
import requests

from app.core.api_log import make_response_hook
from app.core.config import settings

logger = logging.getLogger(__name__)

_http = requests.Session()
_http.hooks["response"].append(make_response_hook("StockService"))
_redis = redis.Redis.from_url(
    settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
)

_CACHE_PREFIX = "stockservice:product:"


def _leer_cache(sku: str) -> dict | None:
    try:
        raw = _redis.get(f"{_CACHE_PREFIX}{sku}")
    except Exception as exc:
        logger.warning("No se pudo leer caché de Stock-Service: %s", exc)
        return None
    return json.loads(raw) if raw else None


def _guardar_cache(sku: str, datos: dict) -> None:
    try:
        _redis.setex(f"{_CACHE_PREFIX}{sku}", settings.STOCK_SERVICE_CACHE_TTL_SECONDS, json.dumps(datos))
    except Exception as exc:
        logger.warning("No se pudo guardar en caché de Stock-Service: %s", exc)


def obtener_producto(sku: str) -> dict:
    """
    GET /api/v1/stock/products/{sku}. Devuelve siempre un dict con forma
    {sku, sap, woo, recent_logs} — si el SKU no existe, sap=None y woo=[]
    (Stock-Service no usa 404 para esto). Usa caché en Redis.
    """
    cacheado = _leer_cache(sku)
    if cacheado is not None:
        return cacheado

    url = f"{settings.STOCK_SERVICE_URL.rstrip('/')}/api/v1/stock/products/{sku}"
    respuesta = _http.get(url, headers={"X-API-Key": settings.STOCK_SERVICE_API_KEY}, timeout=15)
    respuesta.raise_for_status()

    datos = respuesta.json()
    _guardar_cache(sku, datos)
    return datos