"""
Modelo WooOrder — snapshot inmutable de un pedido pagado de WooCommerce
('processing'), tal como llega en el momento del polling. Puerto
simplificado de app/orders/models/woo_order.py de Integrify-Consola: sin
WooOrderItem/WooCustomer como tablas separadas — direcciones e ítems se
guardan como JSON (ver plan.md, Modelo de datos).
"""

from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.models.mixins import SyncStatusMixin


class WooOrder(SyncStatusMixin, table=True):
    __tablename__ = "woo_orders"

    id: int | None = Field(default=None, primary_key=True)

    code: int = Field(index=True, unique=True)  # id de WooCommerce
    reference: int  # number de WooCommerce (visible al cliente)
    paid_at: datetime | None = Field(default=None)
    total: int
    discount: int = Field(default=0)
    shipping: int = Field(default=0)
    pay_auth_code: str | None = Field(default=None, max_length=100)

    delivery_method_code: str | None = Field(default=None, max_length=100)
    bill_doc_type_code: str | None = Field(default=None, max_length=100)
    customer_tax_id: str | None = Field(default=None, max_length=20)

    billing_address: dict = Field(default_factory=dict, sa_column=Column(JSON))
    shipping_address: dict = Field(default_factory=dict, sa_column=Column(JSON))
    items: list = Field(default_factory=list, sa_column=Column(JSON))