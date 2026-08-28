import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.core.config import settings

_IS_CELERY_WORKER = os.getenv("CELERY_WORKER", "").strip() not in ("", "0", "false", "False")

def _fix_url(url: str) -> str:
    """postgresql:// / postgres:// → postgresql+asyncpg://; ?sslmode=require → ?ssl=require."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    url = url.replace("sslmode=require", "ssl=require")
    return url

if _IS_CELERY_WORKER:
    engine = create_async_engine(
        _fix_url(settings.DATABASE_URL),
        echo=settings.LOG_LEVEL.upper() == "DEBUG",
        poolclass=NullPool,
    )
else:
    engine = create_async_engine(
        _fix_url(settings.DATABASE_URL),
        echo=settings.LOG_LEVEL.upper() == "DEBUG",
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

AsyncSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency de FastAPI: async def endpoint(session: AsyncSession = Depends(get_session))."""
    async with AsyncSessionLocal() as session:
        yield session


async def create_db_and_tables() -> None:
    """Crea las tablas de los modelos SQLModel. Solo para desarrollo — en producción
    el esquema lo gestiona Alembic (BQI-04)."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)