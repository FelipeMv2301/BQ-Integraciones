"""Tests de app.pipelines.cleanup::flush_api_logs — con SQLite en memoria, sin red."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.api_log import ApiLog
from app.pipelines import cleanup


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


async def test_sin_entradas_no_hace_nada(session, monkeypatch):
    monkeypatch.setattr(cleanup, "drain_api_logs", lambda max_items: [])

    resultado = await cleanup.flush_api_logs(session)

    assert resultado == {"flushed": 0}


async def test_persiste_entradas_drenadas(session, monkeypatch):
    entradas = [
        {
            "api_name": "SAP", "method": "GET", "url": "https://x.com/a",
            "status_code": 200, "response_time_ms": 12.5,
            "request_body": None, "response_body": None, "error_message": None,
            "created_at": "2026-08-18T10:00:00",
        },
        {
            "api_name": "WooCommerce", "method": "GET", "url": "https://x.com/b",
            "status_code": 500, "response_time_ms": 30.0,
            "request_body": None, "response_body": "error", "error_message": "boom",
            "created_at": "2026-08-18T10:05:00",
        },
    ]
    monkeypatch.setattr(cleanup, "drain_api_logs", lambda max_items: entradas)

    resultado = await cleanup.flush_api_logs(session)

    assert resultado == {"flushed": 2}
    filas = (await session.execute(select(ApiLog))).scalars().all()
    assert {f.api_name for f in filas} == {"SAP", "WooCommerce"}
    fallida = next(f for f in filas if f.api_name == "WooCommerce")
    assert fallida.status_code == 500
    assert fallida.error_message == "boom"
    assert fallida.created_at.isoformat() == "2026-08-18T10:05:00"
