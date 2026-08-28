"""Pedidos de WooCommerce — puerto de services/woocommerce/order.py de Integrify-Consola."""

import requests

from app.services.woocommerce import client

_ESTADO_PROCESANDO = "processing"


def obtener_pedidos(modified_after: str | None = None) -> list[dict]:
    """
    Trae pedidos en estado 'processing' (pagados, listos para facturar).
    modified_after: fecha ISO 8601 — trae solo lo modificado desde ahí.
    """
    params = {"status": _ESTADO_PROCESANDO}
    if modified_after:
        params["modified_after"] = modified_after
    return client.obtener_todas_las_paginas("orders", params=params)

def obtener_pedido(code: int) -> dict | None:
    """GET /orders/{code}. None si el pedido no existe (404) en WooCommerce."""
    try:
        respuesta = client.obtener_pagina(f"orders/{code}", params={})
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return respuesta.json()