"""
Pipeline de ingesta de pedidos — payload normalizado de BioCommerce PRO
(`/wp-json/bio-commerce/v1/`), único origen del proyecto (2026-09-02: se
retiró el path nativo de WooCommerce que leía meta_data a mano, rompía cada
vez que el checkout cambiaba de mecanismo, ver
memory/project_woo_payload_cambiante.md). Transforma pedidos crudos en
filas de woo_orders.

Cada dato de negocio se extrae en su propia función chica — si el checkout
mueve un campo, se ajusta solo esa función, sin tocar el resto del pipeline.
"""

import logging
from datetime import UTC, datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.woo_order import WooOrder
from app.services.biocommerce import orders as biocommerce_api
from app.utils.rut import es_rut_valido, normalizar_rut

logger = logging.getLogger(__name__)


def _normalizar_tax_id_ingesta(tax_id_crudo: str | None) -> str | None:
    """
    Normaliza el RUT (sin puntos, guion, DV mayúscula) al guardarlo en
    woo_orders -- así toda comparación aguas abajo contra sap_customers.tax_id
    (billing.py, invoices.py, orchestrator.py, failures.py) matchea sin que
    cada una tenga que acordarse de normalizar por su cuenta (bug real
    encontrado 2026-09-02: create_sap_invoice comparaba tax_id normalizado
    contra customer_tax_id crudo, nunca encontraba al cliente ya resuelto).

    Si no es un RUT válido se guarda tal cual -- la validación real sigue
    siendo responsabilidad de resolve_customer() (I2: no se aborta la
    ingesta del lote completo por un RUT roto de un solo pedido).
    """
    if tax_id_crudo and es_rut_valido(tax_id_crudo):
        return normalizar_rut(tax_id_crudo)
    return tax_id_crudo


def _extraer_paid_at(payload: dict) -> datetime | None:
    """payment.paid_at llega con offset (ej. '+00:00') — la columna es
    TIMESTAMP WITHOUT TIME ZONE, hay que despojarlo del tzinfo tras
    normalizar a UTC."""
    valor = (payload.get("payment") or {}).get("paid_at")
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError:
        logger.warning("_pedido_a_woo_order: payment.paid_at con formato inesperado: %r", valor)
        return None
    if fecha.tzinfo is not None:
        fecha = fecha.astimezone(UTC).replace(tzinfo=None)
    return fecha


def _extraer_pay_auth_code(payload: dict) -> str | None:
    valor = (payload.get("payment") or {}).get("transaction_id")
    return str(valor).strip() or None if valor else None


def _extraer_delivery_method_code(payload: dict) -> str | None:
    """
    courier_code (ej. 'SGchistn'), no method_id — el sitio manda TODO su
    despacho bajo un único method_id genérico ('bio_shipping_pro'); lo que
    de verdad distingue el courier es courier_code, que además ya coincide
    con el ItemCode real en SAP (confirmado contra SAP prod, ver
    campos-payload-sap.md).
    """
    return (payload.get("shipping") or {}).get("courier_code") or None


def _extraer_doc_type_code(payload: dict) -> str | None:
    codigo = (payload.get("tax_document") or {}).get("sii_code")
    return str(codigo) if codigo is not None else None


def _extraer_tax_id(payload: dict) -> str | None:
    return _normalizar_tax_id_ingesta((payload.get("tax_document") or {}).get("tax_id"))


def _extraer_orden_compra(payload: dict) -> str | None:
    """
    tax_document.orden_compra (nuevo, 2026-09-04) -- opcional, viene vacío/
    ausente en la mayoría de los pedidos. Cuando viene con contenido, se
    mapea a U_FolioRef/U_TpoDoc/U_FchRef en la factura SAP (ver
    services/sap/billing.py::BillingPayload.build).
    """
    valor = (payload.get("tax_document") or {}).get("orden_compra")
    return str(valor).strip() or None if valor else None


def _billing_address(payload: dict) -> dict:
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


def _shipping_address(payload: dict) -> dict:
    shipping = payload.get("shipping_address") or {}
    return {
        "first_name": shipping.get("first_name") or "",
        "last_name": shipping.get("last_name") or "",
        "address_1": shipping.get("address_1") or "",
        "address_2": shipping.get("address_2") or "",
        "state": shipping.get("comuna_code"),
    }


def _extraer_items(payload: dict) -> list[dict]:
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


def _pedido_a_woo_order(payload: dict) -> WooOrder:
    """Adaptador para el payload normalizado de BioCommerce PRO."""
    orden = payload["order"]
    totals = payload["totals"]

    return WooOrder(
        code=orden["id"],
        reference=int(orden["number"]),
        paid_at=_extraer_paid_at(payload),
        total=int(float(totals["total"])),
        discount=int(float(totals["discount_total"])),
        shipping=int(float(totals["shipping_total"])),
        pay_auth_code=_extraer_pay_auth_code(payload),
        delivery_method_code=_extraer_delivery_method_code(payload),
        bill_doc_type_code=_extraer_doc_type_code(payload),
        customer_tax_id=_extraer_tax_id(payload),
        purchase_order_code=_extraer_orden_compra(payload),
        billing_address=_billing_address(payload),
        shipping_address=_shipping_address(payload),
        items=_extraer_items(payload),
    )


async def poll_woo_orders(
    session: AsyncSession, date_from: str, date_to: str, status: str | None = None,
) -> dict:
    """
    Ingesta de pedidos nuevos vía BioCommerce PRO (dedup por code + circuit
    breaker I3, aislado por pedido -- I2).

    Antes este loop no tenía try/except por pedido: uno solo malformado
    (tipo de dato inesperado en un campo, ver memoria del proyecto sobre el
    payload de Woo cambiando entre sitios) lanzaba ANTES de llegar al
    `commit()`, y ninguno de los pedidos del ciclo se guardaba, ni siquiera
    los válidos — Integrify-Consola (legado) sí aísla por pedido
    (`for/try/except/continue`), acá no se estaba igualando ese
    aislamiento (hallazgo real, auditoría 2026-09-02).

    Un pedido que falla al mapear no se marca de ninguna forma especial:
    simplemente no se persiste, así que sigue "nuevo" (no dedupeado) y se
    reintenta solo en el próximo ciclo — si el problema es realmente
    permanente (dato inválido de por vida en ese pedido), va a loguear
    error cada ciclo hasta que alguien lo revise, sin bloquear al resto.
    """
    pedidos_crudos = biocommerce_api.obtener_pedidos(date_from=date_from, date_to=date_to, status=status)

    codigos_existentes = set(
        (await session.execute(select(WooOrder.code))).scalars().all()
    )
    pedidos_nuevos = [p for p in pedidos_crudos if p["order"]["id"] not in codigos_existentes]

    alerta_volumen = len(pedidos_nuevos) > settings.MAX_ORDERS_PER_CYCLE
    if alerta_volumen:
        logger.warning(
            "poll_woo_orders (I3): %d pedidos nuevos supera MAX_ORDERS_PER_CYCLE=%d — "
            "posible bug de polling/dedup, revisar; se procesan igual",
            len(pedidos_nuevos), settings.MAX_ORDERS_PER_CYCLE,
        )

    guardados = fallidos = 0
    for pedido in pedidos_nuevos:
        try:
            session.add(_pedido_a_woo_order(pedido))
            guardados += 1
        except Exception as exc:
            fallidos += 1
            logger.error(
                "poll_woo_orders: no se pudo mapear un pedido, se omite este ciclo (reintenta el "
                "próximo, no bloquea al resto del lote) — %s", exc,
            )

    await session.commit()
    logger.info(
        "poll_woo_orders: %d pedidos nuevos guardados (de %d traídos, %d fallidos al mapear)",
        guardados, len(pedidos_crudos), fallidos,
    )
    return {
        "traidos": len(pedidos_crudos), "nuevos": guardados, "fallidos": fallidos,
        "alerta_volumen": alerta_volumen,
    }
