"""Pedidos del sitio nuevo (bioquimica.devwebs.cl) — API nativa de WooCommerce."""

import requests

from app.services.woocommerce_nuevo import client


def obtener_pedido(code: int) -> dict | None:
    """GET /orders/{code}. None si el pedido no existe (404)."""
    try:
        respuesta = client.obtener_pagina(f"orders/{code}", params={})
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return respuesta.json()
