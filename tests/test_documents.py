"""Tests de app.pipelines.documents — con SQLite en memoria, sin red."""

import base64

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.models.sap_invoice import SAPInvoice
from app.pipelines import documents


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


async def _factura(session) -> SAPInvoice:
    factura = SAPInvoice(doc_entry=1, folio=42742, doc_type_code="33")
    session.add(factura)
    await session.commit()
    return factura


async def test_estado_exitoso_guarda_pdf_decodificado_y_marca_completed(session, monkeypatch):
    pdf_real = b"%PDF-1.4 contenido"
    codificado_una_vez = base64.b64encode(pdf_real).decode("utf-8")
    codificado_dos_veces = base64.b64encode(codificado_una_vez.encode("utf-8")).decode("utf-8")

    monkeypatch.setattr(
        documents.facele_client, "obtener_documento",
        lambda doc_type_code, folio: {"estado": 1, "descripcion": "Proceso OK", "pdf": codificado_dos_veces},
    )

    factura = await _factura(session)
    resultado = await documents.fetch_pdf(session, factura)

    assert resultado.status == "COMPLETED"
    assert resultado.pdf_base64 == codificado_una_vez
    assert resultado.attempts == 1


async def test_estado_0_marca_failed_y_lanza_transient_error(session, monkeypatch):
    monkeypatch.setattr(
        documents.facele_client, "obtener_documento",
        lambda doc_type_code, folio: {"estado": 0, "descripcion": "Folio no encontrado", "pdf": None},
    )

    factura = await _factura(session)
    with pytest.raises(documents.TransientError):
        await documents.fetch_pdf(session, factura)

    assert factura.status == "FAILED"
    assert factura.pdf_base64 is None


async def test_estado_1_sin_pdf_marca_failed_y_lanza_transient_error(session, monkeypatch):
    monkeypatch.setattr(
        documents.facele_client, "obtener_documento",
        lambda doc_type_code, folio: {"estado": 1, "descripcion": "OK", "pdf": None},
    )

    factura = await _factura(session)
    with pytest.raises(documents.TransientError):
        await documents.fetch_pdf(session, factura)

    assert factura.status == "FAILED"


async def test_pdf_malformado_marca_failed_y_lanza_permanent_error(session, monkeypatch):
    monkeypatch.setattr(
        documents.facele_client, "obtener_documento",
        lambda doc_type_code, folio: {"estado": 1, "descripcion": "OK", "pdf": "no-es-base64-!!!"},
    )

    factura = await _factura(session)
    with pytest.raises(documents.PermanentError):
        await documents.fetch_pdf(session, factura)

    assert factura.status == "FAILED"
    assert factura.pdf_base64 is None


async def test_error_de_red_marca_failed_y_lanza_transient_error(session, monkeypatch):
    def _lanzar(doc_type_code, folio):
        raise ConnectionError("Facele/Docele inalcanzable")

    monkeypatch.setattr(documents.facele_client, "obtener_documento", _lanzar)

    factura = await _factura(session)
    with pytest.raises(documents.TransientError):
        await documents.fetch_pdf(session, factura)

    assert factura.status == "FAILED"
