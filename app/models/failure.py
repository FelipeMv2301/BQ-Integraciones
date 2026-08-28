"""
Modelo Failure (BQI-61) — registro visible de reintentos agotados (I6).
Se crea UNA fila cada vez que una entidad de trabajo (WooOrder, SAPBilling,
SAPInvoice, Email) agota sus intentos máximos en una fase del pipeline —
nunca se actualiza una fila existente, cada agotamiento es un evento nuevo
(historial, no estado actual). Sin SyncStatusMixin: no es una entidad que
se sincroniza, es un log (mismo criterio que reference_data.py).
"""

from datetime import datetime

from sqlalchemy import Text
from sqlmodel import Column, Field, SQLModel

from app.utils.dates import utc_now_naive


class Failure(SQLModel, table=True):
    __tablename__ = "failures"

    id: int | None = Field(default=None, primary_key=True)

    entity_type: str = Field(index=True, max_length=50)
    entity_id: int = Field(index=True)
    stage: str = Field(max_length=100)
    error_message: str = Field(sa_column=Column(Text, nullable=False))
    attempts: int

    occurred_at: datetime = Field(default_factory=utc_now_naive)
    notified: bool = Field(default=False, index=True)