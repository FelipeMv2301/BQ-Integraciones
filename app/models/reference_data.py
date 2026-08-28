"""
Catálogos de referencia (datos casi estáticos) — cargados una sola vez
(BQI-21, export puntual desde Integrify-Consola, ya ejecutado) y consultados
por el pipeline en tiempo real, nunca escritos por él. Sin SyncStatusMixin:
no son entidades que se sincronizan una por una, son tablas de mapeo.
"""

from sqlmodel import Field, SQLModel


class Municipality(SQLModel, table=True):
    __tablename__ = "municipalities"

    id: int | None = Field(default=None, primary_key=True)
    woo_code: str = Field(index=True, unique=True, max_length=20)
    sap_code: str = Field(max_length=20)
    name: str = Field(max_length=255)         # SAPMunicipality.name -> "County" en el payload SAP
    city_name: str = Field(max_length=255)    # SAPMunicipality.city_name -> "City"
    state_code: str = Field(max_length=100)   # SAPMunicipality.state_code -> "State"

class Industry(SQLModel, table=True):
    __tablename__ = "industries"

    id: int | None = Field(default=None, primary_key=True)
    woo_code: str = Field(index=True, unique=True, max_length=20)
    sap_code: str = Field(max_length=20)
    name: str = Field(max_length=100)

class DeliveryMethod(SQLModel, table=True):
    __tablename__ = "delivery_methods"

    id: int | None = Field(default=None, primary_key=True)
    woo_code: str = Field(index=True, unique=True, max_length=100)
    sap_sku: str | None = Field(default=None, max_length=100)
    name: str = Field(max_length=255)

class BillDocumentType(SQLModel, table=True):
    __tablename__ = "bill_document_types"

    id: int | None = Field(default=None, primary_key=True)
    woo_code: str = Field(index=True, unique=True, max_length=100)
    sap_code: str = Field(max_length=100)
    name: str = Field(max_length=255)