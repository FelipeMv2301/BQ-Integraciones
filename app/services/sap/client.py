"""
Cliente HTTP para SAP Business One Service Layer (OData v1), sobre la
sesión de Token-SAP-BQ (app/services/sap/session.py).

Nunca se autentica directo contra SAP: usa las cookies de
session.obtener_cookies() y las adjunta a cada request. Ante un 401 (sesión
vencida), invalida en Token-SAP-BQ y reintenta UNA vez con la sesión nueva.
"""

import logging
from urllib.parse import quote

import requests

from app.core.api_log import make_response_hook
from app.core.config import settings
from app.services.sap import session

logger = logging.getLogger(__name__)

_http = requests.Session()
_http.hooks["response"].append(make_response_hook("SAP"))

def _url_odata(endpoint: str, params: dict | None) -> str:
    """
    Construye la URL con los parámetros OData SIN encodear el '$'.

    requests, si le pasás params=..., codifica todo — '$filter' se
    convierte en '%24filter'. SAP no reconoce esa forma codificada y
    responde 400. Por eso armamos la query string a mano.
    """
    base = settings.SAP_URL.rstrip("/")
    url = f"{base}/{endpoint.lstrip('/')}"
    if not params:
        return url

    partes = []
    for clave, valor in params.items():
        clave_enc = quote(str(clave), safe="$")
        valor_enc = quote(str(valor), safe="$'(),")
        partes.append(f"{clave_enc}={valor_enc}")
    return f"{url}?{'&'.join(partes)}"

def solicitar(
    metodo: str,
    endpoint: str,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: int = 60,
) -> requests.Response:
    url = _url_odata(endpoint, params)
    cookies = session.obtener_cookies()

    respuesta = _http.request(metodo, url, cookies=cookies, json=json_body, headers=headers, timeout=timeout)

    if respuesta.status_code == 401:
        logger.warning("SAP devolvió 401 en %s %s — invalidando sesión...", metodo, endpoint)
        session.invalidar(cookies.get("B1SESSION", ""))
        cookies = session.obtener_cookies()
        respuesta = _http.request(metodo, url, cookies=cookies, json=json_body, headers=headers, timeout=timeout)

    return respuesta

def obtener_todas_las_paginas(
    endpoint: str,
    params: dict | None = None,
    items_por_pagina: int = 100,
) -> list[dict]:
    """
    GET paginado usando $skip. El header Prefer le pide a SAP que devuelva
    items_por_pagina resultados por página — sin esto, SAP usa su propio
    tamaño de página por defecto (Integrify-Consola hace lo mismo, ver
    update_items_per_page en services/sap/order.py del original).
    """
    params_base = dict(params or {})
    headers = {"Prefer": f"odata.maxpagesize={items_por_pagina}"}
    todos: list[dict] = []
    skip = 0

    while True:
        params_pagina = {**params_base, "$skip": str(skip)}
        respuesta = solicitar("GET", endpoint, params=params_pagina, headers=headers)
        respuesta.raise_for_status()
        datos = respuesta.json()
        items = datos.get("value", [])
        todos.extend(items)

        if len(items) < items_por_pagina:
            break
        skip += items_por_pagina

    return todos