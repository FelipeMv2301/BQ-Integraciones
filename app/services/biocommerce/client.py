"""
Cliente HTTP para BioCommerce PRO (bioquimica.devwebs.cl) — endpoint propio
normalizado del sitio nuevo, `/wp-json/bio-commerce/v1/`. Reemplaza al
cliente transicional que leía la API nativa de WooCommerce +meta_data a mano
(app/services/woocommerce_nuevo, retirado) — el 401 del permission_callback
del plugin ya se resolvió del lado de Angelo, confirmado 2026-09-01.

Mismas credenciales que el cliente nativo del sitio nuevo (WOO_NUEVO_*): es
el mismo WooCommerce, BioCommerce PRO reutiliza su propia autenticación
ck_/cs_, solo cambia el endpoint.
"""

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

from app.core.api_log import make_response_hook
from app.core.config import settings

_http = requests.Session()
_http.auth = HTTPBasicAuth(settings.WOO_NUEVO_KEY, settings.WOO_NUEVO_SECRET)
_http.hooks["response"].append(make_response_hook("BioCommerce"))

_retry = Retry(
    total=3, backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_http.mount("https://", HTTPAdapter(max_retries=_retry))
_http.mount("http://", HTTPAdapter(max_retries=_retry))


def _url(endpoint: str) -> str:
    base = settings.WOO_NUEVO_URL.rstrip("/")
    return f"{base}/wp-json/bio-commerce/v1/{endpoint.lstrip('/')}"


def obtener(endpoint: str, params: dict | None = None) -> requests.Response:
    respuesta = _http.get(_url(endpoint), params=params or {}, timeout=30)
    respuesta.raise_for_status()
    return respuesta
