"""
Cliente de sesión SAP vía Token-SAP-BQ.

Este módulo NUNCA hace POST /Login directo contra SAP — solo consume
/session y /session/invalidate de Token-SAP-BQ (ver API.md de ese proyecto).
La sesión se cachea en Redis (compartida entre api/worker/beat) con un TTL
derivado de expires_at; si Redis no está disponible, se degrada a una
variable en memoria del proceso — nunca se cae a un login directo (I5).
"""

import json
import logging
from datetime import datetime

import redis
import requests

from app.core.api_log import make_response_hook
from app.core.config import settings

logger = logging.getLogger(__name__)

_REDIS_KEY = "sap:tokensapbq_session"
_EXPIRY_MARGIN_SECONDS = 60  # se considera vencida 60s antes de expires_at

_http = requests.Session()
_http.hooks["response"].append(make_response_hook("TokenSAP"))
_redis = redis.Redis.from_url(
    settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
)
_mem_cache: dict | None = None  # fallback si Redis falla

class TokenSAPBQError(Exception):
    """Token-SAP-BQ no respondió o rechazó la autenticación."""

def _leer_cache() -> dict | None:
    try:
        raw = _redis.get(_REDIS_KEY)
    except Exception as exc:
        logger.warning("No se pudo leer sesión SAP desde Redis: %s", exc)
        return _mem_cache
    if raw is None:
        return _mem_cache
    try:
        return json.loads(raw)
    except Exception:
        return None

def _guardar_cache(data: dict, ttl_segundos: int) -> None:
    global _mem_cache
    _mem_cache = data
    try:
        _redis.setex(_REDIS_KEY, max(ttl_segundos, 1), json.dumps(data))
    except Exception as exc:
        logger.warning("No se pudo guardar sesión SAP en Redis: %s", exc)


def _ttl_desde_expiracion(expires_at_iso: str) -> int:
    try:
        expira = datetime.fromisoformat(expires_at_iso)
        restante = (expira - datetime.now(expira.tzinfo)).total_seconds()
    except Exception:
        restante = 20 * 60  # fallback conservador si el formato es inesperado
    return max(int(restante) - _EXPIRY_MARGIN_SECONDS, 1)

def _pedir_sesion_nueva() -> dict:
    """POST /session — pide (o reutiliza) la sesión compartida de Token-SAP-BQ."""
    url = f"{settings.TOKEN_SAP_BQ_URL.rstrip('/')}/session"
    try:
        response = _http.post(
            url,
            json={
                "service_name": settings.TOKEN_SAP_BQ_SERVICE_NAME,
                "password": settings.TOKEN_SAP_BQ_PASSWORD,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise TokenSAPBQError(f"Token-SAP-BQ inalcanzable: {exc}") from exc

    if not response.ok:
        raise TokenSAPBQError(
            f"Token-SAP-BQ /session -> {response.status_code}: {response.text[:500]}"
        )

    data = response.json()
    sesion = {
        "b1session": data["b1session"],
        "routeid": data.get("routeid"),
        "sap_db": data.get("sap_db"),
        "expires_at": data.get("expires_at"),
    }
    ttl = _ttl_desde_expiracion(sesion["expires_at"]) if sesion["expires_at"] else 25 * 60
    _guardar_cache(sesion, ttl)
    logger.info("Sesión SAP obtenida de Token-SAP-BQ (expira %s)", sesion.get("expires_at"))
    return sesion

def _como_cookies(sesion: dict) -> dict[str, str]:
    cookies = {"B1SESSION": sesion["b1session"]}
    if sesion.get("routeid"):
        cookies["ROUTEID"] = sesion["routeid"]
    return cookies


def obtener_cookies() -> dict[str, str]:
    """
    Devuelve las cookies {B1SESSION, ROUTEID} listas para pegar directo al
    Service Layer de SAP. Usa la caché si sigue vigente; si no, pide una
    nueva a Token-SAP-BQ.
    """
    cache = _leer_cache()
    if cache and cache.get("b1session"):
        return _como_cookies(cache)

    sesion = _pedir_sesion_nueva()
    return _como_cookies(sesion)


def invalidar(b1session: str) -> None:
    """
    POST /session/invalidate — se llama SOLO cuando SAP responde 401 usando
    esa sesión. Limpia también la caché local para forzar pedir una nueva
    en el siguiente obtener_cookies().
    """
    global _mem_cache
    _mem_cache = None
    try:
        _redis.delete(_REDIS_KEY)
    except Exception:
        pass

    url = f"{settings.TOKEN_SAP_BQ_URL.rstrip('/')}/session/invalidate"
    try:
        _http.post(
            url,
            json={
                "service_name": settings.TOKEN_SAP_BQ_SERVICE_NAME,
                "password": settings.TOKEN_SAP_BQ_PASSWORD,
                "b1session": b1session,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("No se pudo invalidar sesión en Token-SAP-BQ: %s", exc)
