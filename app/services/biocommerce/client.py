"""
Cliente HTTP para BioCommerce PRO — endpoint propio normalizado del sitio
activo, `/wp-json/bio-commerce/v1/`. Único origen de pedidos del proyecto
(2026-09-02: se retiró el path nativo de WooCommerce que leía meta_data a
mano, rompía cada vez que el checkout cambiaba de mecanismo).

Usa WOO_URL/WOO_KEY/WOO_SECRET — una sola URL para todo el proyecto, se
cambia en el .env según el sitio activo (test/prod), no mapeando variables
separadas por sitio. Asume que ese WooCommerce tiene BioCommerce PRO
instalado (endpoint /wp-json/bio-commerce/v1/), no la API nativa.
"""

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

from app.core.api_log import make_response_hook
from app.core.config import settings

_http = requests.Session()
_http.auth = HTTPBasicAuth(settings.WOO_KEY, settings.WOO_SECRET)
_http.hooks["response"].append(make_response_hook("BioCommerce"))

_retry = Retry(
    total=3, backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_http.mount("https://", HTTPAdapter(max_retries=_retry))
_http.mount("http://", HTTPAdapter(max_retries=_retry))


def _url(endpoint: str) -> str:
    base = settings.WOO_URL.rstrip("/")
    return f"{base}/wp-json/bio-commerce/v1/{endpoint.lstrip('/')}"


def obtener(endpoint: str, params: dict | None = None) -> requests.Response:
    respuesta = _http.get(_url(endpoint), params=params or {}, timeout=30)
    respuesta.raise_for_status()
    return respuesta
