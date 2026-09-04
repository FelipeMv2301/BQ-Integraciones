"""
Modelo SAPBilling — facturación a crear en SAP, por lote (hasta 21 ítems)
de un WooOrder. Puerto simplificado de app/orders/models/sap_billing.py
de Integrify-Consola.
"""

from datetime import date

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import SyncStatusMixin


class SAPBilling(SyncStatusMixin, table=True):
    __tablename__ = "sap_billings"
    __table_args__ = (UniqueConstraint("woo_order_id", "chunk_index"),)

    id: int | None = Field(default=None, primary_key=True)
    woo_order_id: int = Field(foreign_key="woo_orders.id", index=True)
    chunk_index: int

    doc_type_code: str | None = Field(default=None, max_length=20)
    total: int
    doc_date: date
    internal_notes: str | None = Field(default=None, max_length=100)
    public_notes: str | None = Field(default=None, max_length=254)
    pay_auth_code: str | None = Field(default=None, max_length=100)
    purchase_order_code: str | None = Field(default=None, max_length=100)

    items: list = Field(default_factory=list, sa_column=Column(JSON))

    doc_entry: int | None = Field(default=None)
    doc_num: int | None = Field(default=None)