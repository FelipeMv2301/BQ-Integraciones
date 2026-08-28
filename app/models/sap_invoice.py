"""
Modelo SAPInvoice — factura/boleta con folio ya asignado por el SII (vía
SAP), con el PDF obtenido de Facele/Docele. Puerto simplificado de
app/invoices/models/sap_invoice.py de Integrify-Consola.
"""

from sqlalchemy import Column, Text
from sqlmodel import Field

from app.models.mixins import SyncStatusMixin


class SAPInvoice(SyncStatusMixin, table=True):
    __tablename__ = "sap_invoices"

    id: int | None = Field(default=None, primary_key=True)

    sap_billing_id: int | None = Field(default=None, foreign_key="sap_billings.id", index=True)
    doc_entry: int = Field(index=True, unique=True)
    doc_num: int | None = Field(default=None)
    folio: int | None = Field(default=None)
    folio_prefix: str | None = Field(default=None, max_length=20)
    doc_type_code: str | None = Field(default=None, max_length=20)

    customer_email: str | None = Field(default=None, max_length=100)
    contact_email: str | None = Field(default=None, max_length=200)
    seller_email: str | None = Field(default=None, max_length=100)

    pdf_base64: str | None = Field(default=None, sa_column=Column(Text))