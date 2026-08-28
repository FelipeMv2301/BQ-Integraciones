"""
Cliente HTTP para la API nativa de WooCommerce del sitio nuevo
(bioquimica.devwebs.cl) — transicional, mientras se resuelve el permiso
401 del endpoint normalizado de BioCommerce PRO
(`/wp-json/bio-commerce/v1/orders/{id}/payload`). Mismo patrón que
app/services/woocommerce/client.py (sitio actual), sesión/credenciales
separadas a propósito — son dos tiendas WooCommerce distintas.
"""

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

from app.core.api_log import make_response_hook
from app.core.config import settings

_http = requests.Session()
_http.auth = HTTPBasicAuth(settings.WOO_NUEVO_KEY, settings.WOO_NUEVO_SECRET)
_http.hooks["response"].append(make_response_hook("WooCommerceNuevo"))

_retry = Retry(
    total=3, backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_http.mount("https://", HTTPAdapter(max_retries=_retry))
_http.mount("http://", HTTPAdapter(max_retries=_retry))


def _url(endpoint: str) -> str:
    base = settings.WOO_NUEVO_URL.rstrip("/")
    return f"{base}/wp-json/wc/v3/{endpoint.lstrip('/')}"


def obtener_pagina(endpoint: str, params: dict) -> requests.Response:
    respuesta = _http.get(_url(endpoint), params=params, timeout=30)
    respuesta.raise_for_status()
    return respuesta
