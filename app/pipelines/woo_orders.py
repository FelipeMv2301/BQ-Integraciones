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
from app.services.biocommerce import orders as biocommerce_api
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


# ── Sitio nuevo (bioquimica.devwebs.cl) — vía BioCommerce PRO ───────────
#
# Payload ya normalizado por el plugin propio de Angelo (GET
# /wp-json/bio-commerce/v1/orders/{id}/payload), confirmado funcionando
# 2026-09-01. Reemplaza al adaptador transicional que leía meta_data a mano
# sobre la API nativa de WooCommerce (rompía cada vez que el checkout cambiaba
# de mecanismo, ver historial de este archivo) — acá el propio plugin ya
# resuelve RUT, tipo de documento, giro y código de comuna.


def _bc_extraer_paid_at(payload: dict) -> datetime | None:
    valor = (payload.get("payment") or {}).get("paid_at")
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        logger.warning("_pedido_biocommerce_a_woo_order: payment.paid_at con formato inesperado: %r", valor)
        return None


def _bc_extraer_pay_auth_code(payload: dict) -> str | None:
    valor = (payload.get("payment") or {}).get("transaction_id")
    return str(valor).strip() or None if valor else None


def _bc_extraer_delivery_method_code(payload: dict) -> str | None:
    """
    courier_code (ej. 'SGchistn'), no method_id — el sitio nuevo manda TODO
    su despacho bajo un único method_id genérico ('bio_shipping_pro'); lo que
    de verdad distingue el courier es courier_code, que además ya coincide
    con el ItemCode real en SAP (confirmado contra SAP prod, ver
    campos-payload-sap.md).
    """
    return (payload.get("shipping") or {}).get("courier_code") or None


def _bc_extraer_doc_type_code(payload: dict) -> str | None:
    codigo = (payload.get("tax_document") or {}).get("sii_code")
    return str(codigo) if codigo is not None else None


def _bc_billing_address(payload: dict) -> dict:
    """
    construir_datos_cliente() espera 'state' con el CÓDIGO de comuna (no el
    nombre) — BioCommerce lo manda aparte como comuna_code, separado del
    state/region legible para humanos.
    """
    tax_document = payload.get("tax_document") or {}
    billing = payload.get("billing_address") or {}
    return {
        "company": tax_document.get("business_name") or billing.get("company") or "",
        "first_name": billing.get("first_name") or "",
        "last_name": billing.get("last_name") or "",
        "phone": billing.get("phone") or "",
        "email": billing.get("email") or "",
        "address_1": billing.get("address_1") or "",
        "address_2": billing.get("address_2") or "",
        "state": billing.get("comuna_code"),
        "business_activity": tax_document.get("business_activity"),
        "industry_id": tax_document.get("business_activity_code"),
    }


def _bc_shipping_address(payload: dict) -> dict:
    shipping = payload.get("shipping_address") or {}
    return {
        "first_name": shipping.get("first_name") or "",
        "last_name": shipping.get("last_name") or "",
        "address_1": shipping.get("address_1") or "",
        "address_2": shipping.get("address_2") or "",
        "state": shipping.get("comuna_code"),
    }


def _bc_items(payload: dict) -> list[dict]:
    return [
        {
            "sku": producto.get("sku"),
            "product_id": producto.get("product_id"),
            "quantity": producto.get("quantity"),
            "price": producto.get("unit_price"),
            "total": producto.get("total_before_tax"),
            "total_tax": producto.get("tax"),
        }
        for producto in payload.get("products") or []
    ]


def _pedido_biocommerce_a_woo_order(payload: dict) -> WooOrder:
    """Adaptador para el payload normalizado de BioCommerce PRO."""
    orden = payload["order"]
    totals = payload["totals"]

    return WooOrder(
        code=orden["id"],
        reference=int(orden["number"]),
        paid_at=_bc_extraer_paid_at(payload),
        total=int(totals["total"]),
        discount=int(totals["discount_total"]),
        shipping=int(totals["shipping_total"]),
        pay_auth_code=_bc_extraer_pay_auth_code(payload),
        delivery_method_code=_bc_extraer_delivery_method_code(payload),
        bill_doc_type_code=_bc_extraer_doc_type_code(payload),
        customer_tax_id=(payload.get("tax_document") or {}).get("tax_id"),
        billing_address=_bc_billing_address(payload),
        shipping_address=_bc_shipping_address(payload),
        items=_bc_items(payload),
    )


async def poll_biocommerce_orders(
    session: AsyncSession, date_from: str, date_to: str, status: str | None = None,
) -> dict:
    """
    Ingesta de pedidos del sitio nuevo vía BioCommerce PRO — equivalente a
    poll_woo_orders() pero para bioquimica.devwebs.cl. Mismo criterio de
    dedup por code y circuit breaker I3. NO está conectada a Beat todavía
    a propósito — mismo .env (WOO_NUEVO_*) que usaría el desarrollo en
    paralelo con el sitio viejo si se conectara sin querer en producción.
    """
    pedidos_crudos = biocommerce_api.obtener_pedidos(date_from=date_from, date_to=date_to, status=status)

    codigos_existentes = set(
        (await session.execute(select(WooOrder.code))).scalars().all()
    )
    pedidos_nuevos = [p for p in pedidos_crudos if p["order"]["id"] not in codigos_existentes]

    alerta_volumen = len(pedidos_nuevos) > settings.MAX_ORDERS_PER_CYCLE
    if alerta_volumen:
        logger.warning(
            "poll_biocommerce_orders (I3): %d pedidos nuevos supera MAX_ORDERS_PER_CYCLE=%d — "
            "posible bug de polling/dedup, revisar; se procesan igual",
            len(pedidos_nuevos), settings.MAX_ORDERS_PER_CYCLE,
        )

    for pedido in pedidos_nuevos:
        session.add(_pedido_biocommerce_a_woo_order(pedido))

    await session.commit()
    logger.info(
        "poll_biocommerce_orders: %d pedidos nuevos guardados (de %d traídos)",
        len(pedidos_nuevos), len(pedidos_crudos),
    )
    return {"traidos": len(pedidos_crudos), "nuevos": len(pedidos_nuevos), "alerta_volumen": alerta_volumen}


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