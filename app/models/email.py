"""
Modelo Email — notificación enviada por Brevo: factura al cliente
(CUSTOMER_INVOICE, con PDF adjunto) o alerta interna de fallo definitivo
(INTERNAL_ALERT, sin adjunto). Puerto simplificado de
app/notification/models/email.py de Integrify-Consola — sin Strapi: los
destinatarios fijos (BCC de factura, destinatarios de alerta) vienen de
core/config.py en vez de una tabla de configuración.
"""

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.models.mixins import SyncStatusMixin


class Email(SyncStatusMixin, table=True):
    __tablename__ = "emails"

    id: int | None = Field(default=None, primary_key=True)

    event_type: str = Field(max_length=30)
    sap_invoice_id: int | None = Field(default=None, foreign_key="sap_invoices.id", index=True)
    woo_order_id: int | None = Field(default=None, foreign_key="woo_orders.id", index=True)

    to: list = Field(default_factory=list, sa_column=Column(JSON))
    bcc: list = Field(default_factory=list, sa_column=Column(JSON))
    subject: str | None = Field(default=None, max_length=255)
    brevo_message_id: str | None = Field(default=None, max_length=100)