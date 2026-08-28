"""
Pipeline de ingesta de pedidos WooCommerce (BQI-31). Transforma pedidos
crudos de la API (estado 'processing') en filas de woo_orders.

Cada dato de negocio se extrae en su propia función chica — si el checkout
nuevo mueve un campo, se ajusta solo esa función (ver
memory/project_woo_payload_cambiante.md), sin tocar el resto del pipeline.
"""

import logging
from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.woo_order import WooOrder
from app.services.woocommerce import orders as woo_orders_api

logger = logging.getLogger(__name__)


def _extraer_reference(pedido: dict) -> int:
    return int(pedido["number"])


def _extraer_paid_at(pedido: dict) -> datetime | None:
    """date_paid_gmt llega como string ISO — la columna es datetime, no texto."""
    valor = pedido.get("date_paid_gmt")
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        logger.warning("poll_woo_orders: date_paid_gmt con formato inesperado: %r", valor)
        return None


def _extraer_total(pedido: dict) -> int:
    return int(float(pedido["total"]))


def _extraer_discount(pedido: dict) -> int:
    return int(float(pedido.get("discount_total") or 0))


def _extraer_shipping(pedido: dict) -> int:
    return int(float(pedido.get("shipping_total") or 0))


def _extraer_pay_auth_code(pedido: dict) -> str | None:
    """Prioridad: pay_authorization_code > transaction_id (mismo criterio que Integrify-Consola)."""
    codigo = pedido.get("pay_authorization_code") or pedido.get("transaction_id") or ""
    return str(codigo).strip() or None


def _extraer_metodo_entrega(pedido: dict) -> str | None:
    for linea in pedido.get("shipping_lines", []):
        return linea.get("method_id") or None
    return None


def _extraer_tax_id(pedido: dict) -> str | None:
    return (pedido.get("billing") or {}).get("tax_id") or None


def _extraer_doc_type_code(pedido: dict) -> str | None:
    return (pedido.get("billing") or {}).get("document_type") or None


def _extraer_direccion(pedido: dict, clave: str) -> dict:
    """clave: 'billing' o 'shipping' — se guarda tal cual, sin transformar."""
    return pedido.get(clave) or {}


def _extraer_items(pedido: dict) -> list[dict]:
    return pedido.get("line_items") or []


def _pedido_a_woo_order(pedido: dict) -> WooOrder:
    return WooOrder(
        code=pedido["id"],
        reference=_extraer_reference(pedido),
        paid_at=_extraer_paid_at(pedido),
        total=_extraer_total(pedido),
        discount=_extraer_discount(pedido),
        shipping=_extraer_shipping(pedido),
        pay_auth_code=_extraer_pay_auth_code(pedido),
        delivery_method_code=_extraer_metodo_entrega(pedido),
        bill_doc_type_code=_extraer_doc_type_code(pedido),
        customer_tax_id=_extraer_tax_id(pedido),
        billing_address=_extraer_direccion(pedido, "billing"),
        shipping_address=_extraer_direccion(pedido, "shipping"),
        items=_extraer_items(pedido),
    )


# ── Sitio nuevo (bioquimica.devwebs.cl) — transicional ──────────────────
#
# Mismo pedido "crudo" de WooCommerce, pero tax_id/document_type/industria/
# giro se movieron de `billing.*` a `meta_data` (confirmado 2026-08-21
# comparando la API nativa del sitio nuevo contra el sitio actual). El resto
# del payload (total, ítems, envío, direcciones) es idéntico — se reutilizan
# las mismas funciones de extracción de arriba.
#
# _DOC_TYPE_MAP: el sitio nuevo manda el código interno de documento
# ("BE"/"FE", heredado de Integrify) en vez del código SII directo que
# mandaba el sitio actual ("33"/"39"). Confirmado con Angelo (BioCommerce,
# 2026-08-21): Factura=33, Boleta=39 — faltan las variantes exentas si
# llegan a usarse.
_DOC_TYPE_MAP = {"FE": "33", "BE": "39"}


def _meta(pedido: dict, clave: str) -> str | None:
    for entrada in pedido.get("meta_data") or []:
        if entrada.get("key") == clave:
            return entrada.get("value") or None
    return None


def _extraer_tax_id_nuevo(pedido: dict) -> str | None:
    return _meta(pedido, "_billing_tax_id") or _meta(pedido, "billing_tax_id")


def _extraer_doc_type_code_nuevo(pedido: dict) -> str | None:
    crudo = _meta(pedido, "_billing_doc_type") or _meta(pedido, "billing_doc_type")
    return _DOC_TYPE_MAP.get(crudo, crudo)


def _extraer_industry_nuevo(pedido: dict) -> str | None:
    return _meta(pedido, "_billing_industry") or _meta(pedido, "billing_industry")


def _extraer_business_activity_nuevo(pedido: dict) -> str | None:
    return _meta(pedido, "_billing_business_activity") or _meta(pedido, "billing_business_activity")


def _pedido_nuevo_a_woo_order(pedido: dict) -> WooOrder:
    """
    Igual que _pedido_a_woo_order, pero para el sitio nuevo. `industry_id`/
    `business_activity` se inyectan dentro de `billing_address` (en vez de
    columnas propias) para que construir_datos_cliente() los siga leyendo
    sin cambios — solución transicional mientras se define el contrato
    definitivo con BioCommerce.
    """
    billing = dict(_extraer_direccion(pedido, "billing"))
    billing["industry_id"] = _extraer_industry_nuevo(pedido)
    billing["business_activity"] = _extraer_business_activity_nuevo(pedido)

    return WooOrder(
        code=pedido["id"],
        reference=_extraer_reference(pedido),
        paid_at=_extraer_paid_at(pedido),
        total=_extraer_total(pedido),
        discount=_extraer_discount(pedido),
        shipping=_extraer_shipping(pedido),
        pay_auth_code=_extraer_pay_auth_code(pedido),
        delivery_method_code=_extraer_metodo_entrega(pedido),
        bill_doc_type_code=_extraer_doc_type_code_nuevo(pedido),
        customer_tax_id=_extraer_tax_id_nuevo(pedido),
        billing_address=billing,
        shipping_address=_extraer_direccion(pedido, "shipping"),
        items=_extraer_items(pedido),
    )


async def poll_woo_orders(session: AsyncSession, modified_after: str | None = None) -> dict:
    """
    Trae pedidos 'processing' nuevos desde WooCommerce y los guarda en
    woo_orders (dedup por code). Circuit breaker I3: si el lote de nuevos
    supera MAX_ORDERS_PER_CYCLE, se alerta (log de warning por ahora — se
    conecta a notify_failure en BQI-53) pero se procesa igual, no se aborta.
    """
    pedidos_crudos = woo_orders_api.obtener_pedidos(modified_after=modified_after)

    codigos_existentes = set(
        (await session.execute(select(WooOrder.code))).scalars().all()
    )
    pedidos_nuevos = [p for p in pedidos_crudos if p["id"] not in codigos_existentes]

    alerta_volumen = len(pedidos_nuevos) > settings.MAX_ORDERS_PER_CYCLE
    if alerta_volumen:
        logger.warning(
            "poll_woo_orders (I3): %d pedidos nuevos supera MAX_ORDERS_PER_CYCLE=%d — "
            "posible bug de polling/dedup, revisar; se procesan igual",
            len(pedidos_nuevos), settings.MAX_ORDERS_PER_CYCLE,
        )

    for pedido in pedidos_nuevos:
        session.add(_pedido_a_woo_order(pedido))

    await session.commit()
    logger.info("poll_woo_orders: %d pedidos nuevos guardados (de %d traídos)", len(pedidos_nuevos), len(pedidos_crudos))
    return {"traidos": len(pedidos_crudos), "nuevos": len(pedidos_nuevos), "alerta_volumen": alerta_volumen}