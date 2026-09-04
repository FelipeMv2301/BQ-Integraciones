"""
Facturación en SAP Business One — POST /Invoices. Construido sobre
app.services.sap.client (sesión, reintento en 401). Puerto de
services/sap/schemas/billing.py + services/sap/billing.py de
Integrify-Consola.
"""

from pydantic import BaseModel, Field, computed_field

from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.services.sap import client
from app.utils.dates import hoy_chile

# Código fijo que SAP espera en U_TpoDocRef para "orden de compra de terceros"
# (tax_document.orden_compra de BioCommerce, ver woo_orders.py) -- pedido
# explícito de Felipe, 2026-09-04.
_TPO_DOC_ORDEN_COMPRA = "801"

_ENDPOINT = "Invoices"
SAP_TAX_CODE = "IVA"
SAP_SELLER_CODE = "25"


class BillingItemPayload(BaseModel):
    sku: str = Field(alias="ItemCode")
    qty: int = Field(alias="Quantity")
    price: int = Field(alias="UnitPrice")
    discount: int = Field(default=0, alias="DiscountPercent")
    total: int = Field(alias="LineTotal")
    warehouse_code: str = Field(alias="WarehouseCode")
    expense_center_code: str = Field(default="CG3060", alias="CostingCode")
    cost_center_code: str = Field(default="CC1000", alias="CostingCode2")
    tax_group_code: str = Field(default=SAP_TAX_CODE, alias="VatGroup")
    tax_code: str = Field(default=SAP_TAX_CODE, alias="TaxCode")

    model_config = {"populate_by_name": True}


class BillingPayload(BaseModel):
    doc_type_code: str = Field(alias="U_IX_Ind")
    customer_code: str = Field(alias="CardCode")
    contact_code: str = Field(alias="CntctCode")
    ship_code: str = Field(alias="ShipToCode")
    bill_code: str = Field(alias="PayToCode")
    doc_date: str = Field(alias="DocDate")
    tax_date: str = Field(alias="TaxDate")
    commit_date: str = Field(alias="DocDueDate")
    pay_group_code: int = Field(default=-1, alias="PaymentGroupCode")
    total: int = Field(alias="DocTotal")
    seller_code: str = Field(default=SAP_SELLER_CODE, alias="SalesPersonCode")
    sales_area: str = Field(default="TRA", alias="U_BQ_AREA")
    web_order: str = Field(default="Y", alias="U_VentaWeb")
    order_type: str = Field(default="WEB", alias="U_TipoVenta")
    order_num: int = Field(alias="U_WedDocNum")
    internal_notes: str = Field(alias="Comments")
    public_notes: str = Field(alias="U_NX_Observacion")
    pay_auth_code: str | None = Field(default=None, alias="U_BQ_CodVoucher")
    # Orden de compra de terceros (tax_document.orden_compra de BioCommerce)
    # -- los 3 solo se mandan juntos, cuando el pedido trae ese dato; si no,
    # se omiten los 3 (exclude_none=True en el model_dump del caller).
    purchase_order_ref: str | None = Field(default=None, alias="U_FolioRef")
    purchase_order_doc_type: str | None = Field(default=None, alias="U_TpoDocRef")
    purchase_order_date: str | None = Field(default=None, alias="U_FchRef")
    excluded: str = Field(default="N", alias="U_IXP_EXCLUDED")
    transfer_flag: str = Field(default="N", alias="U_Traspaso_FE")
    transfer_code: str = Field(default="1", alias="U_IndTraslado")
    tax_delivery_code: str = Field(default="2", alias="U_TipoDesp")
    items: list[BillingItemPayload] = Field(alias="DocumentLines")

    model_config = {"populate_by_name": True}

    @computed_field(alias="DocumentSubType")
    @property
    def doc_sub_type(self) -> str:
        return "bod_None" if self.doc_type_code == "33" else "bod_Bill"

    @classmethod
    def build(cls, factura: SAPBilling, cliente: SAPCustomer, order_num: int) -> "BillingPayload":
        """
        R1 (backlog): commit_date = doc_date, NO una fecha de compromiso
        real — los pedidos web son siempre al contado; si se manda otra
        fecha, el DTE imprime "Crédito" en SAP en vez de "Contado". Mismo
        criterio exacto que Integrify-Consola.
        """
        fecha = factura.doc_date.strftime("%Y-%m-%d")

        # Orden de compra: los 3 campos solo se llenan si el pedido trajo
        # tax_document.orden_compra -- U_FchRef es la fecha de HOY (Chile),
        # no doc_date (que es la fecha de pago del pedido, puede ser otro día).
        purchase_order_ref = purchase_order_doc_type = purchase_order_date = None
        if factura.purchase_order_code:
            purchase_order_ref = factura.purchase_order_code
            purchase_order_doc_type = _TPO_DOC_ORDEN_COMPRA
            purchase_order_date = hoy_chile().strftime("%Y-%m-%d")

        return cls(
            doc_type_code=factura.doc_type_code,
            customer_code=cliente.code,
            contact_code=cliente.contact_name,
            ship_code=cliente.ship_code,
            bill_code=cliente.bill_code,
            doc_date=fecha,
            tax_date=fecha,
            commit_date=fecha,
            total=factura.total,
            order_num=order_num,
            internal_notes=factura.internal_notes,
            public_notes=factura.public_notes,
            purchase_order_ref=purchase_order_ref,
            purchase_order_doc_type=purchase_order_doc_type,
            purchase_order_date=purchase_order_date,
            pay_auth_code=factura.pay_auth_code,
            items=[
                BillingItemPayload(
                    sku=item["sku"], qty=item["qty"], price=item["price"],
                    total=item["total"], warehouse_code=item["warehouse_code"],
                )
                for item in factura.items
            ],
        )


def buscar_factura_existente(order_num: int, total: int, doc_type_code: str) -> dict | None:
    """
    Busca en SAP una factura ya creada para este chunk exacto, antes de
    intentar crearla de nuevo. U_WedDocNum (order_num) es el número de
    PEDIDO Woo, no de factura — un mismo pedido se trocea en varios chunks
    (R2) que comparten order_num, así que hace falta combinar también
    DocTotal y U_IX_Ind para identificar el chunk exacto.
    """
    respuesta = client.solicitar(
        "GET", _ENDPOINT,
        params={
            "$filter": (
                f"U_WedDocNum eq '{order_num}' "
                f"and DocTotal eq {total} "
                f"and U_IX_Ind eq '{doc_type_code}'"
            ),
            "$select": "DocEntry,DocNum",
        },
    )
    respuesta.raise_for_status()
    resultados = respuesta.json().get("value", [])
    return resultados[0] if resultados else None


def create_sap_invoice(payload: dict):
    """POST /Invoices — crea la facturación en SAP (todavía sin folio)."""
    return client.solicitar("POST", _ENDPOINT, json_body=payload)