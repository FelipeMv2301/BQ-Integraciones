"""
Modelo SAPCustomer — Business Partner de SAP resuelto o creado a partir de
un cliente de WooCommerce. Puerto de app/customers/models/sap_customer.py
de Integrify-Consola (sin Django) — ver plan.md, sección Modelo de datos.
"""

from sqlmodel import Field

from app.models.mixins import SyncStatusMixin


class SAPCustomer(SyncStatusMixin, table=True):
    __tablename__ = "sap_customers"

    id: int | None = Field(default=None, primary_key=True)

    tax_id: str = Field(index=True, unique=True, max_length=20)
    code: str | None = Field(default=None, max_length=20)
    exists: bool = Field(default=False)

    name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=100)
    business_activity: str | None = Field(default=None, max_length=80)
    industry_sap_code: str | None = Field(default=None, max_length=20)

    contact_code: int | None = Field(default=None)
    contact_name: str | None = Field(default=None, max_length=50)
    contact_first_name: str | None = Field(default=None, max_length=50)
    contact_last_name: str | None = Field(default=None, max_length=50)
    contact_phone: str | None = Field(default=None, max_length=20)
    contact_email: str | None = Field(default=None, max_length=100)

    bill_code: str | None = Field(default=None, max_length=50)
    bill_row: int = Field(default=0)
    bill_address: str | None = Field(default=None, max_length=100)
    bill_municipality_name: str | None = Field(default=None, max_length=255)
    bill_municipality_city_name: str | None = Field(default=None, max_length=255)
    bill_municipality_state_code: str | None = Field(default=None, max_length=100)

    ship_code: str | None = Field(default=None, max_length=50)
    ship_row: int = Field(default=1)
    ship_address: str | None = Field(default=None, max_length=100)
    ship_municipality_name: str | None = Field(default=None, max_length=255)
    ship_municipality_city_name: str | None = Field(default=None, max_length=255)
    ship_municipality_state_code: str | None = Field(default=None, max_length=100)