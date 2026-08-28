"""
Configuración del entorno de Alembic. Modo async porque el engine de la
app es async (asyncpg) — no hay driver sync instalado.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from app.models.sap_customer import SAPCustomer  # noqa: F401
from app.models.reference_data import BillDocumentType, DeliveryMethod, Industry, Municipality  # noqa: F401
from app.models.woo_order import WooOrder  # noqa: F401
from app.models.sap_billing import SAPBilling  # noqa: F401
from app.models.sap_invoice import SAPInvoice  # noqa: F401
from app.models.email import Email  # noqa: F401
from app.models.failure import Failure  # noqa: F401
from app.models.api_log import ApiLog  # noqa: F401

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from app.core.config import settings

# Importar acá cada módulo de modelos a medida que se agreguen (desde E2 en
# adelante), para que Alembic los detecte en autogenerate. Por ahora ninguno.

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _db_url() -> str:
    url = settings.DATABASE_URL
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    url = url.replace("sslmode=require", "ssl=require")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_db_url(), poolclass=None)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())