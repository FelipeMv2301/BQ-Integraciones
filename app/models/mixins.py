"""
Mixins reutilizables para modelos que se sincronizan con un sistema externo
y necesitan reintentos acotados — equivalente a SyncMixin/LoadMixin/
TimestampMixin de Integrify-Consola, sin Django.
"""

from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from app.models.enums import SyncStatus
from app.utils.dates import utc_now_naive


class SyncStatusMixin(SQLModel):
    # sa_type (no sa_column=Column(...)): con sa_column, la MISMA instancia
    # de Column se comparte entre todos los modelos que usan este mixin —
    # SQLAlchemy exige que una columna pertenezca a una sola tabla, así que
    # el segundo modelo que hereda el mixin (WooOrder) fallaría al migrar.
    # Con sa_type, SQLModel construye una columna nueva por cada tabla.
    status: str = Field(
        default=SyncStatus.PENDING.value,
        sa_type=SAEnum(SyncStatus, native_enum=False),
    )
    status_message: str | None = Field(default=None)
    attempts: int = Field(default=0)
    last_attempt_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)

