"""Pedidos vía BioCommerce PRO — payload normalizado propio del sitio nuevo."""

import requests

from app.services.biocommerce import client

_PAGINA_POR_DEFECTO = 100


def obtener_pedido(code: int) -> dict | None:
    """GET /orders/{code}/payload. None si el pedido no existe (404)."""
    try:
        respuesta = client.obtener(f"orders/{code}/payload")
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return respuesta.json()


def obtener_pedidos(
    date_from: str,
    date_to: str,
    date_field: str = "modified",
    status: str | None = None,
    per_page: int = _PAGINA_POR_DEFECTO,
) -> list[dict]:
    """
    GET /orders/payload — paginado por page/per_page (no $skip como SAP),
    recorre hasta agotar pagination.total_pages.
    """
    params = {"date_from": date_from, "date_to": date_to, "date_field": date_field, "per_page": per_page}
    if status:
        params["status"] = status

    todos: list[dict] = []
    pagina = 1
    while True:
        respuesta = client.obtener("orders/payload", params={**params, "page": pagina})
        datos = respuesta.json()
        todos.extend(datos.get("orders", []))

        paginacion = datos.get("pagination") or {}
        if pagina >= paginacion.get("total_pages", 1):
            break
        pagina += 1

    return todos
