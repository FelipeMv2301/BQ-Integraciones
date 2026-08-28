"""Tests de app.pipelines.failure_tracking::escalar_si_agotado — con SQLite en memoria."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.failure import Failure
from app.models.woo_order import WooOrder
from app.pipelines import failure_tracking


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


async def _armar_woo_order(session, attempts, status="FAILED", mensaje="RUT no válido") -> WooOrder:
    orden = WooOrder(
        code=1, reference=100, total=0, items=[], bill_doc_type_code="39",
        status=status, attempts=attempts, status_message=mensaje,
    )
    session.add(orden)
    await session.commit()
    return orden


async def test_no_escala_si_no_llego_al_limite(session, monkeypatch):
    orden = await _armar_woo_order(session, attempts=2)
    llamado = []
    monkeypatch.setattr(failure_tracking, "notify_failure", lambda kind, msg: llamado.append((kind, msg)))

    await failure_tracking.escalar_si_agotado(session, orden, "WooOrder", "prepare_billing", max_attempts=3)

    assert orden.status == "FAILED"
    assert llamado == []
    filas = (await session.execute(select(Failure))).scalars().all()
    assert filas == []


async def test_escala_a_exhausted_y_notifica_al_llegar_al_limite(session, monkeypatch):
    orden = await _armar_woo_order(session, attempts=3, mensaje="RUT no válido: 'abc'")
    llamado = []
    monkeypatch.setattr(failure_tracking, "notify_failure", lambda kind, msg: llamado.append((kind, msg)))

    await failure_tracking.escalar_si_agotado(session, orden, "WooOrder", "prepare_billing", max_attempts=3)

    assert orden.status == "EXHAUSTED"
    assert len(llamado) == 1
    assert llamado[0][0] == "WooOrder:prepare_billing"
    assert "RUT no válido" in llamado[0][1]

    filas = (await session.execute(select(Failure))).scalars().all()
    assert len(filas) == 1
    assert filas[0].entity_type == "WooOrder"
    assert filas[0].entity_id == orden.id
    assert filas[0].stage == "prepare_billing"
    assert filas[0].attempts == 3


async def test_no_reescala_si_ya_estaba_exhausted(session, monkeypatch):
    orden = await _armar_woo_order(session, attempts=5, status="EXHAUSTED")
    llamado = []
    monkeypatch.setattr(failure_tracking, "notify_failure", lambda kind, msg: llamado.append((kind, msg)))

    await failure_tracking.escalar_si_agotado(session, orden, "WooOrder", "prepare_billing", max_attempts=3)

    assert llamado == []
    filas = (await session.execute(select(Failure))).scalars().all()
    assert filas == []
