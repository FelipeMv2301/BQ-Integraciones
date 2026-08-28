"""
Registro de llamadas HTTP a APIs externas — escritura vía cola Redis.
Puerto de Stock-Service/app/core/api_log.py.

PROBLEMA QUE RESUELVE:
  Los clientes HTTP (Token-SAP-BQ, SAP, WooCommerce, Facele, Stock-Service)
  son síncronos (requests), pero la base de datos es async (asyncpg). Un
  cliente síncrono no puede escribir en la DB async sin bloquear o crear un
  event loop.

SOLUCIÓN:
  1. Cada llamada HTTP se encola como JSON en la lista Redis "apilog:queue"
     (operación síncrona de <1 ms, fail-silent si Redis cae).
  2. La tarea Celery task_flush_api_logs drena la cola cada 5 minutos y
     persiste en PostgreSQL (tabla api_logs) en un solo lote.

SEGURIDAD:
  - El body de POST /session y /session/invalidate de Token-SAP-BQ nunca se
    registra (contiene la password del servicio) — es el único cliente de
    este proyecto que manda un secreto en el body; el resto (Facele,
    Stock-Service) los manda por header, y WooCommerce/SAP usan
    auth/cookies, nunca aparecen en el body.
  - Bodies truncados a 2000 caracteres.
"""

import json
import logging
import re

from app.core.config import settings
from app.utils.dates import utc_now_naive

logger = logging.getLogger("api_log")

APILOG_REDIS_KEY = "apilog:queue"
APILOG_QUEUE_MAX = 10_000     # tope de la cola si el flush falla — evita OOM en Redis
BODY_MAX_CHARS = 2_000

# Query params cuyo valor se redacta de las URLs registradas
_REDACT_PARAMS = re.compile(r"(consumer_key|consumer_secret|api[-_]?key)=[^&]+", re.IGNORECASE)

# Endpoints cuyo body nunca se registra (llevan password/credenciales)
_SENSITIVE_PATHS = ("/session", "/session/invalidate")

_redis = None
_redis_failed = False


def _get_redis():
    """Conexión Redis lazy y compartida. None si Redis no está disponible."""
    global _redis, _redis_failed
    if _redis is not None or _redis_failed:
        return _redis
    try:
        import redis
        _redis = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    except Exception as exc:
        _redis_failed = True
        logger.warning("Redis no disponible para ApiLog: %s", exc)
    return _redis


def _redact_url(url: str) -> str:
    """Elimina credenciales de los query params de la URL."""
    return _REDACT_PARAMS.sub(r"\1=***", url)


def _truncate(body: str | None) -> str | None:
    if body is None:
        return None
    return body[:BODY_MAX_CHARS]


def log_api_call(
    api_name: str,
    method: str,
    url: str,
    status_code: int = 0,
    response_time_ms: float = 0,
    request_body: str | None = None,
    response_body: str | None = None,
    error_message: str | None = None,
) -> None:
    """
    Encola un registro de llamada API en Redis. Nunca lanza excepciones —
    un fallo de logging jamás debe romper la llamada de negocio.
    """
    conn = _get_redis()
    if conn is None:
        return
    try:
        entry = json.dumps({
            "api_name": api_name,
            "method": method,
            "url": _redact_url(url)[:2000],
            "status_code": status_code,
            "response_time_ms": round(response_time_ms, 2),
            "request_body": _truncate(request_body),
            "response_body": _truncate(response_body),
            "error_message": error_message[:2000] if error_message else None,
            "created_at": utc_now_naive().isoformat(),
        })
        pipe = conn.pipeline()
        pipe.lpush(APILOG_REDIS_KEY, entry)
        pipe.ltrim(APILOG_REDIS_KEY, 0, APILOG_QUEUE_MAX - 1)
        pipe.execute()
    except Exception:
        pass   # fail-silent por diseño


def drain_api_logs(max_items: int = 1000) -> list[dict]:
    """
    Extrae hasta max_items registros de la cola Redis (los borra de la cola).
    Usado por task_flush_api_logs para persistirlos en PostgreSQL.
    """
    conn = _get_redis()
    if conn is None:
        return []
    try:
        pipe = conn.pipeline()
        pipe.lrange(APILOG_REDIS_KEY, -max_items, -1)
        pipe.ltrim(APILOG_REDIS_KEY, 0, -max_items - 1)
        raw_items, _ = pipe.execute()
        # lrange devuelve en orden de inserción inversa — restaurar cronología
        return [json.loads(item) for item in reversed(raw_items)]
    except Exception as exc:
        logger.warning("No se pudo drenar la cola ApiLog: %s", exc)
        return []


def make_response_hook(api_name: str):
    """
    Crea un hook de respuesta para requests.Session.

    Se engancha con: session.hooks["response"].append(make_response_hook("SAP"))
    Captura método, URL, status, timing y bodies de TODAS las llamadas de la
    sesión, incluyendo los reintentos automáticos del HTTPAdapter.
    """
    def _hook(response, *args, **kwargs):
        try:
            request = response.request
            url = request.url or ""

            is_sensitive = any(url.rstrip("/").endswith(p) for p in _SENSITIVE_PATHS)

            request_body = None
            if not is_sensitive and request.body:
                body = request.body
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                request_body = body

            log_api_call(
                api_name=api_name,
                method=request.method or "?",
                url=url,
                status_code=response.status_code,
                response_time_ms=response.elapsed.total_seconds() * 1000,
                request_body=request_body,
                response_body=response.text if not response.ok else None,
                # Solo guardamos el body de respuesta en errores — los
                # exitosos serían catálogos completos o PDFs enteros.
            )
        except Exception:
            pass
        return response

    return _hook
