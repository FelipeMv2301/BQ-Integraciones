"""
Cliente HTTP para WooCommerce REST API v3 — Basic Auth, reintento con
backoff, paginación por X-WP-TotalPages (más robusto que contar
len(page) < 100: no pierde pedidos si una página exacta de 100 coincide
justo con el corte real).
"""

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

from app.core.api_log import make_response_hook
from app.core.config import settings

_http = requests.Session()
_http.auth = HTTPBasicAuth(settings.WOO_KEY, settings.WOO_SECRET)
_http.hooks["response"].append(make_response_hook("WooCommerce"))

_retry = Retry(
    total=3, backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_http.mount("https://", HTTPAdapter(max_retries=_retry))
_http.mount("http://", HTTPAdapter(max_retries=_retry))


def _url(endpoint: str) -> str:
    base = settings.WOO_URL.rstrip("/")
    return f"{base}/wp-json/wc/v3/{endpoint.lstrip('/')}"


def obtener_pagina(endpoint: str, params: dict) -> requests.Response:
    """GET a WooCommerce. Devuelve la Response completa (no solo el JSON)
    — la paginación necesita leer el header X-WP-TotalPages."""
    respuesta = _http.get(_url(endpoint), params=params, timeout=30)
    respuesta.raise_for_status()
    return respuesta


def obtener_todas_las_paginas(
    endpoint: str, params: dict | None = None, por_pagina: int = 100
) -> list[dict]:
    """Recorre TODAS las páginas usando X-WP-TotalPages — no asume que
    'menos de por_pagina items' es la última página."""
    params_base = {**(params or {}), "per_page": por_pagina}
    resultados = []
    pagina = 1

    while True:
        respuesta = obtener_pagina(endpoint, {**params_base, "page": pagina})
        resultados.extend(respuesta.json())

        total_paginas = int(respuesta.headers.get("X-WP-TotalPages") or 1)
        if pagina >= total_paginas:
            break
        pagina += 1

    return resultados