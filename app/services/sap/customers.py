"""
Consultas a SAP Business One — Business Partners (clientes). Construido
sobre app.services.sap.client (sesión, reintento en 401, encoding OData) —
este módulo solo conoce el endpoint y el filtro de negocio, no cómo se
autentica ni qué hacer ante un 401.
"""

from pydantic import BaseModel, Field

from app.models.sap_customer import SAPCustomer
from app.services.sap import client

_ENDPOINT = "BusinessPartners"

def find_by_rut(rut: str) -> list[dict]:
    """
    Busca Business Partners tipo Cliente (CardType='C', GroupCode=100) cuyo
    FederalTaxID coincida exacto con `rut`, o empiece igual sin los últimos
    2 caracteres — mismo filtro que usa Integrify-Consola, para tolerar
    variaciones menores de formato entre lo guardado en SAP y lo que llega
    de WooCommerce.

    Retorna la lista cruda de resultados de SAP: puede venir vacía (no
    existe), con uno, o —en teoría— con más de uno. Decidir qué hacer con
    eso es responsabilidad de quien llama (BQI-25/26), no de esta función.
    """
    filtro = (
        f"(FederalTaxID eq '{rut}' or startswith(FederalTaxID,'{rut[:-2]}')) "
        "and CardType eq 'C' and GroupCode eq 100"
    )
    respuesta = client.solicitar("GET", _ENDPOINT, params={"$filter": filtro})
    respuesta.raise_for_status()
    return respuesta.json().get("value", [])


SAP_SELLER_CODE = "25"  # ventas WEB — confirmado en utils/constants/sap.py del original
SAP_TAX_CODE = "IVA"
_ADDRESS_TYPE_BILL = "bo_BillTo"
_ADDRESS_TYPE_SHIP = "bo_ShipTo"


class CustomerAddressPayload(BaseModel):
    customer_code: str = Field(alias="BPCode")
    type_code: str = Field(alias="AddressType")
    code: str = Field(alias="AddressName")
    reference: str = Field(default="WEB", alias="AddressName2")
    row_num: int = Field(alias="RowNum")
    address: str = Field(alias="Street")
    municipality_name: str = Field(alias="County")
    city_name: str = Field(alias="City")
    state_code: str = Field(alias="State")
    country_code: str = Field(default="CL", alias="Country")
    tax_code: str = Field(default=SAP_TAX_CODE, alias="TaxCode")

    model_config = {"populate_by_name": True}


class CustomerContactPayload(BaseModel):
    code: int = Field(alias="InternalCode")
    customer_code: str = Field(alias="CardCode")
    name: str = Field(alias="Name")
    reference: str = Field(default="WEB", alias="Remarks1")
    first_name: str = Field(alias="FirstName")
    last_name: str = Field(alias="LastName")
    phone: str = Field(alias="MobilePhone")
    email: str = Field(alias="E_Mail")

    model_config = {"populate_by_name": True}


class CustomerPayload(BaseModel):
    code: str = Field(alias="CardCode")
    name: str = Field(alias="CardName")
    tax_id: str = Field(alias="FederalTaxID")
    business_activity: str | None = Field(default=None, alias="U_NX_GIRO")
    phone: str = Field(alias="Phone1")
    email: str = Field(alias="EmailAddress")
    type_code: str = Field(default="C", alias="CardType")
    group_code: str = Field(default="100", alias="GroupCode")
    seller_code: str = Field(default=SAP_SELLER_CODE, alias="SalesPersonCode")
    debitor_account: str = Field(default="1140101", alias="DebitorAccount")
    contact_name: str = Field(alias="ContactPerson")
    industry_code: str | None = Field(default=None, alias="U_ActEconomica")
    addresses: list[CustomerAddressPayload] = Field(alias="BPAddresses")
    contacts: list[CustomerContactPayload] = Field(alias="ContactEmployees")

    model_config = {"populate_by_name": True}

    @classmethod
    def build(cls, cliente: SAPCustomer) -> "CustomerPayload":
        """Construye el payload a partir de un SAPCustomer ya completo
        (BQI-26 es responsable de haberlo llenado antes de llamar acá)."""
        return cls(
            code=cliente.code,
            name=cliente.name,
            tax_id=cliente.tax_id,
            business_activity=cliente.business_activity,
            phone=cliente.phone,
            email=cliente.email,
            contact_name=cliente.contact_name,
            industry_code=cliente.industry_sap_code,
            addresses=[
                CustomerAddressPayload(
                    customer_code=cliente.code,
                    type_code=_ADDRESS_TYPE_BILL,
                    code=cliente.bill_code,
                    row_num=cliente.bill_row,
                    address=cliente.bill_address,
                    municipality_name=cliente.bill_municipality_name,
                    city_name=cliente.bill_municipality_city_name,
                    state_code=cliente.bill_municipality_state_code,
                ),
                CustomerAddressPayload(
                    customer_code=cliente.code,
                    type_code=_ADDRESS_TYPE_SHIP,
                    code=cliente.ship_code,
                    row_num=cliente.ship_row,
                    address=cliente.ship_address,
                    municipality_name=cliente.ship_municipality_name,
                    city_name=cliente.ship_municipality_city_name,
                    state_code=cliente.ship_municipality_state_code,
                ),
            ],
            contacts=[
                CustomerContactPayload(
                    code=cliente.contact_code,
                    customer_code=cliente.code,
                    name=cliente.contact_name,
                    first_name=cliente.contact_first_name,
                    last_name=cliente.contact_last_name,
                    phone=cliente.contact_phone,
                    email=cliente.contact_email,
                ),
            ],
        )


def create_or_update(existe: bool, payload: dict, code: str | None = None):
    """
    POST si no existe (crea), PATCH si existe (actualiza) — mismo criterio
    que services/sap/customer.py::SAPCustomer.create_or_update del original.
    `code` es obligatorio cuando existe=True (va en la URL de la request).
    """
    endpoint = _ENDPOINT if not existe else f"{_ENDPOINT}('{code}')"
    metodo = "PATCH" if existe else "POST"
    return client.solicitar(metodo, endpoint, json_body=payload)

SAP_BILL_ADDRESS_NAME = "FISCAL"
SAP_SHIP_ADDRESS_NAME = "DESPACHO"
SAP_CONTACT_NAME = "CONTACTO"


def contacto_disponible(contactos: list[dict]) -> dict:
    """
    Busca entre los ContactEmployees ya existentes en SAP uno con
    Remarks1='WEB' (el que este pipeline creó/gestiona) para reutilizarlo.
    Si no hay ninguno, busca un nombre "CONTACTO"/"CONTACTO2"/... libre
    para crear uno nuevo sin chocar con los existentes — mismo criterio
    que Customer.available_contact del original. Esto es justo lo que
    faltó en BQI-25 y provocó el error -2035 al probar contra SAP real.
    """
    nombres_ocupados = set()
    for contacto in contactos:
        nombre = (contacto.get("Name") or "").strip().upper()
        referencia = contacto.get("Remarks1")
        if referencia and referencia.strip().upper() == "WEB":
            return {"code": contacto.get("InternalCode"), "name": contacto.get("Name")}
        nombres_ocupados.add(nombre)

    correlativo = 2
    candidato = SAP_CONTACT_NAME
    while candidato in nombres_ocupados:
        candidato = f"{SAP_CONTACT_NAME}{correlativo}"
        correlativo += 1
    return {"code": 0, "name": candidato}


def direcciones_disponibles(direcciones: list[dict]) -> dict:
    """
    Mismo criterio que Customer.available_addresses del original: para
    BILL y SHIP, reutiliza la dirección con AddressName2='WEB' si existe;
    si no, busca un nombre/fila libres para crear una nueva.
    """
    resultado = {"BILL": None, "SHIP": None}
    filas_ocupadas = {d.get("RowNum") for d in direcciones}
    nombres_ocupados = {(d.get("AddressName") or "").strip().upper() for d in direcciones}

    tipos = {"BILL": ("bo_BillTo", SAP_BILL_ADDRESS_NAME), "SHIP": ("bo_ShipTo", SAP_SHIP_ADDRESS_NAME)}

    for clave, (type_code, nombre_base) in tipos.items():
        for direccion in direcciones:
            referencia = direccion.get("AddressName2")
            if (
                referencia and referencia.strip().upper() == "WEB"
                and direccion.get("AddressType") == type_code
            ):
                resultado[clave] = {"row": direccion.get("RowNum"), "name": direccion.get("AddressName")}
                break

        if not resultado[clave]:
            correlativo = 1
            while True:
                candidato = f"{nombre_base}{correlativo if correlativo > 1 else ''}"
                if candidato not in nombres_ocupados:
                    fila = 0
                    while fila in filas_ocupadas:
                        fila += 1
                    resultado[clave] = {"row": fila, "name": candidato}
                    nombres_ocupados.add(candidato)
                    filas_ocupadas.add(fila)
                    break
                correlativo += 1

    return resultado