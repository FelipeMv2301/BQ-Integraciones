
"""
Pipeline de preparación de facturación (BQI-33). Convierte los ítems de un
WooOrder en uno o más SAPBilling (lotes de hasta 21 líneas + ítem de
envío), resolviendo SKU/bodega contra Stock-Service. Puerto simplificado
de app/orders/management/commands/prepare_sap_billing_sync.py de
Integrify-Consola.
"""

import logging
import math
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytz
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import SyncStatus
from app.models.reference_data import DeliveryMethod
from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.woo_order import WooOrder
from app.pipelines.errors import marcar_fallido
from app.services.sap import billing as sap_billing
from app.services.sap import exchange_rates
from app.services.stockservice.client import obtener_producto

logger = logging.getLogger(__name__)

_TAX_RATE = Decimal("0.19")
_TAMANO_LOTE = 21
_ZONA_CHILE = pytz.timezone("America/Santiago")


class PermanentError(Exception):
    """Error de negocio no reintentable (dato faltante, discrepancia de totales)."""


class TransientError(Exception):
    """Error transitorio (SAP/Stock-Service caído, rechazo temporal) — reintentable."""


def _fecha_pago_chile(paid_at: datetime) -> date:
    utc = paid_at.replace(tzinfo=UTC) if paid_at.tzinfo is None else paid_at
    return utc.astimezone(_ZONA_CHILE).date()


async def _buscar_metodo_entrega(session: AsyncSession, woo_code: str | None) -> DeliveryMethod | None:
    if not woo_code:
        return None
    return (
        await session.execute(select(DeliveryMethod).where(DeliveryMethod.woo_code == woo_code))
    ).scalar_one_or_none()


def _resolver_sku_bodega(item: dict) -> tuple[str, str]:
    """
    Resuelve SKU (ItemCode SAP) y bodega para un ítem del pedido, contra
    Stock-Service. Si hay más de una entrada 'woo' para el SKU (variantes),
    matchea por woo_id == product_id del ítem (confirmado en BQI-32).
    """
    sku = item.get("sku")
    if not sku:
        raise PermanentError(f"Ítem sin SKU: product_id={item.get('product_id')}")

    datos = obtener_producto(sku)
    entradas_woo = datos.get("woo") or []
    if not entradas_woo:
        raise PermanentError(f"SKU {sku!r} no encontrado en Stock-Service")

    product_id = item.get("product_id")
    entrada = next((w for w in entradas_woo if w.get("woo_id") == product_id), entradas_woo[0])
    return sku, entrada.get("sync_warehouse") or "01"


def _item_a_linea(item: dict) -> dict:
    sku, bodega = _resolver_sku_bodega(item)
    return {
        "sku": sku,
        "qty": item["quantity"],
        "price": math.ceil(float(item["price"])),
        "total": int(float(item["total"])),
        "total_tax": int(float(item.get("total_tax") or 0)),
        "warehouse_code": bodega,
    }


def _item_envio(monto_bruto: int, sap_sku: str) -> dict:
    neto = Decimal(monto_bruto) / (1 + _TAX_RATE)
    neto_redondeado = int(neto.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "sku": sap_sku,
        "qty": 1,
        "price": neto_redondeado,
        "total": neto_redondeado,
        "total_tax": monto_bruto - neto_redondeado,
        "warehouse_code": "01",
    }


def _trocear(items: list[dict], tamano: int = _TAMANO_LOTE) -> list[list[dict]]:
    return [items[i:i + tamano] for i in range(0, len(items), tamano)]


async def prepare_billing(session: AsyncSession, woo_order: WooOrder) -> list[SAPBilling]:
    """
    Convierte los ítems de woo_order en uno o más SAPBilling. Idempotente
    por chunk_index (I4): si ya existen filas para este pedido, se
    actualizan en vez de duplicarse.

    Marca woo_order.status=FAILED con el motivo si algo no reintentable
    falla (BQI-34); COMPLETED si el troceo salió bien (esto NO implica que
    ya esté creado en SAP — eso lo marca cada SAPBilling.status por
    separado en BQI-35).
    """
    try:
        if not woo_order.paid_at:
            raise PermanentError(f"WooOrder {woo_order.code} sin paid_at")

        metodo_entrega = await _buscar_metodo_entrega(session, woo_order.delivery_method_code)

        lineas = [_item_a_linea(item) for item in woo_order.items]

        if woo_order.shipping > 0:
            if not metodo_entrega or not metodo_entrega.sap_sku:
                raise PermanentError(
                    f"Sin SKU SAP de envío para método {woo_order.delivery_method_code!r}"
                )
            lineas.append(_item_envio(woo_order.shipping, metodo_entrega.sap_sku))

        lotes = _trocear(lineas)

        # Validar ANTES de tocar la sesión: si hay discrepancia, ningún
        # SAPBilling llega a agregarse -- el except de abajo comitea solo
        # el WooOrder marcado FAILED. Antes esta validación corría DESPUÉS
        # del loop que ya había hecho session.add() de cada chunk nuevo, así
        # que una discrepancia dejaba filas SAPBilling huérfanas persistidas
        # igual (el commit del except las arrastraba) — bug real, auditoría
        # 2026-09-02.
        total_calculado = sum(
            linea["total"] + linea["total_tax"] for lote in lotes for linea in lote
        )
        if total_calculado != woo_order.total:
            raise PermanentError(
                f"Discrepancia de totales: WooOrder={woo_order.total}, SAPBilling={total_calculado}"
            )

        fecha = _fecha_pago_chile(woo_order.paid_at)
        notas = f"Pedido web {woo_order.reference}"

        facturaciones = []
        for indice, lote in enumerate(lotes):
            existente = (
                await session.execute(
                    select(SAPBilling).where(
                        SAPBilling.woo_order_id == woo_order.id,
                        SAPBilling.chunk_index == indice,
                    )
                )
            ).scalar_one_or_none()

            total_lote = sum(linea["total"] + linea["total_tax"] for linea in lote)

            if existente:
                existente.items, existente.total = lote, total_lote
                facturacion = existente
            else:
                facturacion = SAPBilling(
                    woo_order_id=woo_order.id,
                    chunk_index=indice,
                    doc_type_code=woo_order.bill_doc_type_code,
                    total=total_lote,
                    doc_date=fecha,
                    internal_notes=notas,
                    public_notes=notas,
                    pay_auth_code=woo_order.pay_auth_code,
                    purchase_order_code=woo_order.purchase_order_code,
                    items=lote,
                )
                session.add(facturacion)
            facturaciones.append(facturacion)

        woo_order.status = SyncStatus.COMPLETED
        await session.commit()
        return facturaciones

    except PermanentError as exc:
        await marcar_fallido(session, woo_order, str(exc), PermanentError, cause=exc)
    except Exception as exc:
        await marcar_fallido(session, woo_order, f"Error inesperado: {exc}", TransientError, cause=exc)


async def create_sap_invoice(session: AsyncSession, factura: SAPBilling, woo_order: WooOrder) -> SAPBilling:
    """
    Crea la facturación en SAP (BQI-35). Requiere que el cliente ya esté
    resuelto en sap_customers (BQI-26) — si no está, es un error de
    orquestación previo, no algo que esta función deba resolver.

    Idempotente (BQI-37): si esta fila ya está COMPLETED, no vuelve a
    tocar SAP. Si no lo está localmente pero SAP ya tiene la factura
    (crash entre el POST exitoso y el commit), la adopta en vez de
    duplicarla.
    """
    if factura.status == SyncStatus.COMPLETED:
        return factura

    try:
        existente = sap_billing.buscar_factura_existente(
            order_num=woo_order.reference, total=factura.total, doc_type_code=factura.doc_type_code,
        )
    except Exception as exc:
        await marcar_fallido(session, factura, f"Error consultando SAP: {exc}", TransientError, cause=exc)

    if existente:
        factura.doc_entry = existente.get("DocEntry")
        factura.doc_num = existente.get("DocNum")
        factura.status, factura.status_message = SyncStatus.COMPLETED, None
        await session.commit()
        return factura

    cliente = (
        await session.execute(
            select(SAPCustomer).where(SAPCustomer.tax_id == woo_order.customer_tax_id)
        )
    ).scalar_one_or_none()
    if cliente is None or not cliente.code:
        await marcar_fallido(session, factura, "Cliente SAP no resuelto", PermanentError)

    try:
        exchange_rates.asegurar_tasa_cambio(factura.doc_date)
    except Exception as exc:
        await marcar_fallido(session, factura, f"Tasa de cambio: {exc}", TransientError, cause=exc)

    try:
        payload = sap_billing.BillingPayload.build(factura, cliente, woo_order.reference).model_dump(
            by_alias=True, exclude_none=True
        )
    except Exception as exc:
        await marcar_fallido(session, factura, f"Payload inválido: {exc}", PermanentError, cause=exc)

    try:
        respuesta = sap_billing.create_sap_invoice(payload)
    except Exception as exc:
        await marcar_fallido(session, factura, f"Error llamando a SAP: {exc}", TransientError, cause=exc)

    if respuesta.ok:
        datos = respuesta.json()
        factura.doc_entry = datos.get("DocEntry")
        factura.doc_num = datos.get("DocNum")
        factura.status, factura.status_message = SyncStatus.COMPLETED, None
        factura.attempts += 1
        await session.commit()
        return factura

    await marcar_fallido(
        session, factura, f"SAP {respuesta.status_code}: {respuesta.text[:500]}", TransientError
    )