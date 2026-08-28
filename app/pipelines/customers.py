"""
Pipeline de resolución de cliente SAP (BQI-26). Equivalente a
prepare_sap_customers_sync.py + sync_sap_customers.py de Integrify-Consola,
unidos en una sola función: valida RUT, busca o crea/actualiza el Business
Partner en SAP, y deja el resultado persistido en sap_customers.
"""

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import SyncStatus
from app.models.reference_data import Industry, Municipality
from app.models.sap_customer import SAPCustomer
from app.models.woo_order import WooOrder
from app.services.sap import customers as sap_customers
from app.utils.rut import es_rut_valido, normalizar_rut
from app.utils.sap_text import sanitizar_texto_sap


class PermanentError(Exception):
    """Error de negocio no reintentable (RUT inválido, payload mal armado)."""


class TransientError(Exception):
    """Error transitorio (SAP caído, rechazo temporal) — reintentable."""


async def resolve_customer(session: AsyncSession, tax_id: str, datos_cliente: dict) -> SAPCustomer:
    """
    datos_cliente espera: name, phone, email, business_activity,
    industry_sap_code, contact_first_name, contact_last_name, contact_phone,
    contact_email, bill_address, bill_municipality_{name,city_name,state_code},
    ship_address, ship_municipality_{name,city_name,state_code}.
    """
    if not es_rut_valido(tax_id):
        raise PermanentError(f"RUT no válido: {tax_id!r}")

    tax_id_normalizado = normalizar_rut(tax_id)

    cliente = (
        await session.execute(
            select(SAPCustomer).where(SAPCustomer.tax_id == tax_id_normalizado)
        )
    ).scalar_one_or_none()
    if cliente is None:
        cliente = SAPCustomer(tax_id=tax_id_normalizado)
        session.add(cliente)

    cliente.status = SyncStatus.IN_PROGRESS
    await session.commit()

    try:
        resultados = sap_customers.find_by_rut(tax_id_normalizado)
    except Exception as exc:
        cliente.status, cliente.status_message = SyncStatus.FAILED, f"Error consultando SAP: {exc}"
        cliente.attempts += 1
        await session.commit()
        raise TransientError(str(exc)) from exc

    _completar_datos_sap(cliente, resultados, tax_id_normalizado)
    _completar_datos_propios(cliente, datos_cliente)

    try:
        payload = sap_customers.CustomerPayload.build(cliente).model_dump(
            by_alias=True, exclude_none=True
        )
    except Exception as exc:
        cliente.status, cliente.status_message = SyncStatus.FAILED, f"Payload inválido: {exc}"
        cliente.attempts += 1
        await session.commit()
        raise PermanentError(str(exc)) from exc

    respuesta = sap_customers.create_or_update(existe=cliente.exists, payload=payload, code=cliente.code)
    cliente.attempts += 1

    if respuesta.ok:
        cliente.status, cliente.status_message = SyncStatus.COMPLETED, None
        await session.commit()
        return cliente

    cliente.status = SyncStatus.FAILED
    cliente.status_message = f"SAP {respuesta.status_code}: {respuesta.text[:500]}"
    await session.commit()
    raise TransientError(cliente.status_message)


def _completar_datos_sap(cliente: SAPCustomer, resultados: list[dict], tax_id: str) -> None:
    """Decide code/exists/contact/direcciones según si el BP ya existe en SAP."""
    if resultados:
        cliente_sap = resultados[0]
        contacto = sap_customers.contacto_disponible(cliente_sap.get("ContactEmployees", []))
        direcciones = sap_customers.direcciones_disponibles(cliente_sap.get("BPAddresses", []))
        cliente.code = cliente_sap["CardCode"]
        cliente.exists = True
        cliente.contact_code = contacto["code"]
        cliente.contact_name = contacto["name"]
        cliente.bill_code = direcciones["BILL"]["name"]
        cliente.bill_row = direcciones["BILL"]["row"]
        cliente.ship_code = direcciones["SHIP"]["name"]
        cliente.ship_row = direcciones["SHIP"]["row"]
    else:
        cliente.code = f"CN{tax_id}"
        cliente.exists = False
        cliente.contact_code = 0
        cliente.contact_name = sap_customers.SAP_CONTACT_NAME
        cliente.bill_code = sap_customers.SAP_BILL_ADDRESS_NAME
        cliente.bill_row = 0
        cliente.ship_code = sap_customers.SAP_SHIP_ADDRESS_NAME
        cliente.ship_row = 1


def _completar_datos_propios(cliente: SAPCustomer, datos: dict) -> None:
    """Copia los datos del pedido/cliente al SAPCustomer, sanitizados (BQI-24)."""
    cliente.name = sanitizar_texto_sap(datos["name"], max_length=100)
    cliente.phone = sanitizar_texto_sap(datos["phone"], max_length=20)
    cliente.email = sanitizar_texto_sap(datos["email"], max_length=100)
    actividad = datos.get("business_activity")
    cliente.business_activity = sanitizar_texto_sap(actividad, max_length=80) if actividad else None
    cliente.industry_sap_code = datos.get("industry_sap_code")

    cliente.contact_first_name = sanitizar_texto_sap(datos["contact_first_name"], max_length=50)
    cliente.contact_last_name = sanitizar_texto_sap(datos["contact_last_name"], max_length=50)
    cliente.contact_phone = sanitizar_texto_sap(datos["contact_phone"], max_length=20)
    cliente.contact_email = sanitizar_texto_sap(datos["contact_email"], max_length=100)

    cliente.bill_address = sanitizar_texto_sap(datos["bill_address"], max_length=100)
    cliente.bill_municipality_name = datos["bill_municipality_name"]
    cliente.bill_municipality_city_name = datos["bill_municipality_city_name"]
    cliente.bill_municipality_state_code = datos["bill_municipality_state_code"]

    cliente.ship_address = sanitizar_texto_sap(datos["ship_address"], max_length=100)
    cliente.ship_municipality_name = datos["ship_municipality_name"]
    cliente.ship_municipality_city_name = datos["ship_municipality_city_name"]
    cliente.ship_municipality_state_code = datos["ship_municipality_state_code"]


async def _buscar_comuna(session: AsyncSession, woo_code: str | None) -> Municipality:
    comuna = None
    if woo_code:
        comuna = (
            await session.execute(select(Municipality).where(Municipality.woo_code == woo_code))
        ).scalar_one_or_none()
    if comuna is None:
        raise PermanentError(f"Comuna Woo sin mapear a SAP: {woo_code!r}")
    return comuna


async def _buscar_giro_sap(session: AsyncSession, woo_code: str | None) -> str | None:
    if not woo_code:
        return None
    industria = (
        await session.execute(select(Industry).where(Industry.woo_code == woo_code))
    ).scalar_one_or_none()
    if industria is None:
        raise PermanentError(f"Giro/industria Woo sin mapear a SAP: {woo_code!r}")
    return industria.sap_code


def _direccion(bloque: dict) -> str:
    """address_1 + address_2, sin separador — igual que sap_customer.py::_sanitize_bill_address del original."""
    return f"{bloque.get('address_1', '')}{bloque.get('address_2') or ''}"


async def construir_datos_cliente(session: AsyncSession, woo_order: WooOrder) -> dict:
    """
    Arma el dict que resolve_customer() espera, a partir del snapshot
    crudo en woo_order.billing_address/shipping_address (BQI-31). Puerto
    de app/customers/models/sap_customer.py::clean() de Integrify-Consola.

    Quirks replicados a propósito:
    - name: company (si existe) o "first_name last_name" — nunca ambos.
    - contact_first_name/last_name vienen de SHIPPING (a quién se entrega),
    pero contact_email/contact_phone vienen de BILLING — así arma
    Integrify el "contacto" de SAP.
    - Dirección = address_1+address_2 sin espacio entre medio.

    Comuna/giro sin mapear -> PermanentError (falta agregar la fila al
    catálogo, reintentar no lo arregla solo).
    """
    billing = woo_order.billing_address or {}
    shipping = woo_order.shipping_address or {}

    nombre = billing.get("company") or f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()

    bill_comuna = await _buscar_comuna(session, billing.get("state"))
    ship_comuna = await _buscar_comuna(session, shipping.get("state"))

    return {
        "name": nombre,
        "phone": billing.get("phone") or "",
        "email": billing.get("email") or "",
        "business_activity": billing.get("business_activity"),
        "industry_sap_code": await _buscar_giro_sap(session, billing.get("industry_id")),
        "contact_first_name": shipping.get("first_name", ""),
        "contact_last_name": shipping.get("last_name", ""),
        "contact_phone": billing.get("phone") or "",
        "contact_email": billing.get("email") or "",
        "bill_address": _direccion(billing),
        "bill_municipality_name": bill_comuna.name,
        "bill_municipality_city_name": bill_comuna.city_name,
        "bill_municipality_state_code": bill_comuna.state_code,
        "ship_address": _direccion(shipping),
        "ship_municipality_name": ship_comuna.name,
        "ship_municipality_city_name": ship_comuna.city_name,
        "ship_municipality_state_code": ship_comuna.state_code,
    }
