"""
Verificación y carga automática de la tasa de cambio USD del día en SAP
(R3/I5 — SAP rechaza cualquier documento si falta la tasa de la fecha
EXACTA del documento, DocDate, no necesariamente "hoy").

Confirmado contra SAP real (2026-08-18):
- SBOBobService_GetCurrencyRate(Currency, Date) -> Edm.Double. Sin tasa,
  responde 400 con code=-4006 ("Update the exchange rate").
- SBOBobService_SetCurrencyRate(RateDate, Currency, Rate) -> 204 al cargar.
- Los parámetros van en el BODY JSON (json_body), no en query params —
  a pesar de ser FunctionImport, no siguen el estilo $filter de las
  entidades normales.

Fuente del valor: mindicador.cl (mismo proveedor que ya usa Stock-Service
para su propio cálculo de precios — aunque ese caso solo LEE, nunca
escribe en SAP).
"""

import logging
from datetime import date

import requests

from app.core.config import settings
from app.services.sap import client

logger = logging.getLogger(__name__)

_MINDICADOR_URL = "https://mindicador.cl/api/dolar"
_HEADERS_MINDICADOR = {"User-Agent": "Mozilla/5.0"}

_SIN_TASA_CODE = -4006
_CACHE_PREFIX = "sap:tasa_cambio_ok:"
_CACHE_TTL_SECONDS = 20 * 60 * 60  # 20h — cubre el resto del día sin sobrevivir a mañana


class TasaCambioError(Exception):
    """No se pudo confirmar ni cargar la tasa de cambio."""


def _cache_key(fecha: date) -> str:
    return f"{_CACHE_PREFIX}{fecha.isoformat()}"


def _leer_cache(fecha: date) -> bool:
    try:
        import redis
        r = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
        )
        return bool(r.get(_cache_key(fecha)))
    except Exception as exc:
        logger.warning("asegurar_tasa_cambio: Redis no disponible para caché (%s)", exc)
        return False


def _guardar_cache(fecha: date) -> None:
    try:
        import redis
        r = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
        )
        r.setex(_cache_key(fecha), _CACHE_TTL_SECONDS, "1")
    except Exception as exc:
        logger.warning("asegurar_tasa_cambio: no se pudo guardar caché (%s)", exc)


def _existe_tasa(fecha: date) -> bool:
    respuesta = client.solicitar(
        "POST", "SBOBobService_GetCurrencyRate",
        json_body={"Currency": "USD", "Date": fecha.isoformat()},
    )
    if respuesta.ok:
        return True

    try:
        codigo = respuesta.json().get("error", {}).get("code")
    except Exception:
        codigo = None
    if codigo == _SIN_TASA_CODE:
        return False

    respuesta.raise_for_status()  # cualquier otro error, no lo tapamos como "sin tasa"
    return False


def _obtener_tasa_mindicador(fecha: date) -> float:
    url = f"{_MINDICADOR_URL}/{fecha.strftime('%d-%m-%Y')}"
    respuesta = requests.get(url, headers=_HEADERS_MINDICADOR, timeout=10)
    respuesta.raise_for_status()
    serie = respuesta.json().get("serie", [])
    if not serie:
        raise TasaCambioError(f"mindicador.cl sin dato de USD para {fecha.isoformat()}")
    return float(serie[0]["valor"])


def _cargar_tasa(fecha: date, valor: float) -> None:
    respuesta = client.solicitar(
        "POST", "SBOBobService_SetCurrencyRate",
        json_body={"RateDate": fecha.isoformat(), "Currency": "USD", "Rate": str(valor)},
    )
    respuesta.raise_for_status()


def asegurar_tasa_cambio(fecha: date) -> None:
    """
    Confirma que SAP tenga la tasa USD de `fecha` — si no, la trae de
    mindicador.cl y la carga. Cachea la confirmación en Redis (20h) para no
    golpear SAP/mindicador.cl en cada factura del mismo día. Lanza
    TasaCambioError si no se pudo asegurar de ninguna forma.
    """
    if _leer_cache(fecha):
        return

    if _existe_tasa(fecha):
        _guardar_cache(fecha)
        return

    logger.info("asegurar_tasa_cambio: falta tasa USD %s en SAP, cargando desde mindicador.cl", fecha)
    try:
        valor = _obtener_tasa_mindicador(fecha)
        _cargar_tasa(fecha, valor)
    except Exception as exc:
        raise TasaCambioError(f"No se pudo asegurar la tasa USD de {fecha.isoformat()}: {exc}") from exc

    logger.info("asegurar_tasa_cambio: tasa USD %s cargada en SAP (%.2f)", fecha, valor)
    _guardar_cache(fecha)
