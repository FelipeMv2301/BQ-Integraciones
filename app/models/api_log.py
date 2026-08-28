"""
Modelo ApiLog — registro de todas las llamadas HTTP a servicios externos
(Token-SAP-BQ, SAP Service Layer, WooCommerce, Facele/Docele, Stock-Service).
Puerto de Stock-Service/app/models/api_log.py.

FLUJO DE ESCRITURA: los clientes HTTP son síncronos y la DB es async, así
que no escriben aquí directamente. Cada llamada se encola en Redis
(app/core/api_log.py) y la tarea Celery task_flush_api_logs la persiste en
lotes cada 5 minutos.
"""

from datetime import datetime

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from app.utils.dates import utc_now_naive


class ApiLog(SQLModel, table=True):
    __tablename__ = "api_logs"

    id: int | None = Field(default=None, primary_key=True)

    api_name: str = Field(max_length=50, index=True)
    # "TokenSAP" | "SAP" | "WooCommerce" | "Facele" | "StockService"

    method: str = Field(max_length=10)
    url: str = Field(max_length=2000)
    status_code: int = Field(default=0, index=True)
    # 0 = la request nunca obtuvo respuesta (error de red/timeout)

    response_time_ms: float = Field(default=0)

    request_body: str | None = Field(default=None, sa_column=Column(Text))
    response_body: str | None = Field(default=None, sa_column=Column(Text))
    # Truncados a 2000 caracteres; el body de /session de Token-SAP-BQ nunca
    # se registra (lleva la password del servicio, ver app/core/api_log.py)

    error_message: str | None = Field(default=None, max_length=2000)

    created_at: datetime = Field(default_factory=utc_now_naive, index=True)
