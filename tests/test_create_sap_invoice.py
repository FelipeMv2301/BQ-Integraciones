"""Tests de app.pipelines.billing::create_sap_invoice — con SQLite en memoria, sin red."""

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.models.sap_billing import SAPBilling
from app.models.sap_customer import SAPCustomer
from app.models.woo_order import WooOrder
from app.pipelines import billing


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture(autouse=True)
def _sin_factura_existente_en_sap_por_defecto(monkeypatch):
    """
    BQI-37: create_sap_invoice ahora consulta buscar_factura_existente antes
    de crear. Por defecto, en estos tests, "no existe" — los tests que sí
    quieren simular que ya existe la sobreescriben llamando de nuevo a
    monkeypatch.setattr con el mismo `monkeypatch` del test (misma
    instancia, gana el último setattr).
    """
    monkeypatch.setattr(billing.sap_billing, "buscar_factura_existente", lambda **kwargs: None)
    monkeypatch.setattr(billing.exchange_rates, "asegurar_tasa_cambio", lambda fecha: None)


async def _armar(session) -> tuple[SAPBilling, WooOrder]:
    orden = WooOrder(
        code=1, reference=100, total=1190, customer_tax_id="12345678-5",
        items=[], bill_doc_type_code="39",
    )
    session.add(orden)
    await session.commit()

    factura = SAPBilling(
        woo_order_id=orden.id, chunk_index=0, doc_type_code="39", total=1190,
        doc_date=date(2026, 8, 13), internal_notes="Pedido web 100", public_notes="Pedido web 100",
        items=[{"sku": "ML0001", "qty": 1, "price": 1000, "total": 1000, "warehouse_code": "01"}],
    )
    session.add(factura)
    await session.commit()
    return factura, orden


class _Respuesta:
    def __init__(self, ok, status_code=200, data=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


async def test_falla_permanente_si_no_hay_sap_customer(session):
    factura, orden = await _armar(session)

    with pytest.raises(billing.PermanentError):
        await billing.create_sap_invoice(session, factura, orden)

    assert factura.status == "FAILED"
    assert factura.status_message == "Cliente SAP no resuelto"


async def test_sap_ok_guarda_doc_entry_y_marca_completed(session, monkeypatch):
    factura, orden = await _armar(session)
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    ))
    await session.commit()

    monkeypatch.setattr(
        billing.sap_billing, "create_sap_invoice",
        lambda payload: _Respuesta(ok=True, data={"DocEntry": 555, "DocNum": 999}),
    )

    resultado = await billing.create_sap_invoice(session, factura, orden)

    assert resultado.status == "COMPLETED"
    assert resultado.doc_entry == 555
    assert resultado.doc_num == 999


async def test_sap_rechaza_marca_failed_y_lanza_transient_error(session, monkeypatch):
    factura, orden = await _armar(session)
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    ))
    await session.commit()

    monkeypatch.setattr(
        billing.sap_billing, "create_sap_invoice",
        lambda payload: _Respuesta(ok=False, status_code=400, text="dato inválido"),
    )

    with pytest.raises(billing.TransientError):
        await billing.create_sap_invoice(session, factura, orden)

    assert factura.status == "FAILED"
    assert "400" in factura.status_message


async def test_sap_lanza_excepcion_marca_failed_y_sube_attempts(session, monkeypatch):
    """
    Bug real (auditoría 2026-09-02): sap_billing.create_sap_invoice() no
    tenía try/except -- si la llamada lanzaba (timeout, ConnectionError) en
    vez de devolver ok=False, factura.attempts nunca subía y
    escalar_si_agotado() nunca escalaba.
    """
    factura, orden = await _armar(session)
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    ))
    await session.commit()

    def _create_sap_invoice_caido(payload):
        raise ConnectionError("SAP no responde")

    monkeypatch.setattr(billing.sap_billing, "create_sap_invoice", _create_sap_invoice_caido)

    with pytest.raises(billing.TransientError):
        await billing.create_sap_invoice(session, factura, orden)

    assert factura.status == "FAILED"
    assert factura.attempts == 1
    assert "SAP no responde" in factura.status_message


async def test_buscar_factura_existente_lanza_excepcion_marca_failed_y_sube_attempts(session, monkeypatch):
    """Mismo bug, en la consulta previa de idempotencia externa (BQI-37)."""
    factura, orden = await _armar(session)
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    ))
    await session.commit()

    def _buscar_caido(**kwargs):
        raise ConnectionError("SAP no responde")

    monkeypatch.setattr(billing.sap_billing, "buscar_factura_existente", _buscar_caido)

    with pytest.raises(billing.TransientError):
        await billing.create_sap_invoice(session, factura, orden)

    assert factura.status == "FAILED"
    assert factura.attempts == 1


async def test_factura_ya_completed_no_vuelve_a_consultar_sap(session, monkeypatch):
    """Guard barato (BQI-37): si la fila ya está COMPLETED, ni siquiera consulta SAP."""
    factura, orden = await _armar(session)
    factura.status, factura.doc_entry = "COMPLETED", 42
    await session.commit()

    llamadas = []
    monkeypatch.setattr(
        billing.sap_billing, "buscar_factura_existente",
        lambda **kwargs: llamadas.append(kwargs) or None,
    )

    resultado = await billing.create_sap_invoice(session, factura, orden)

    assert resultado is factura
    assert resultado.doc_entry == 42
    assert llamadas == []


async def test_factura_ya_existe_en_sap_adopta_doc_entry_sin_post(session, monkeypatch):
    """
    Guard robusto (BQI-37): si SAP ya tiene la factura (crash entre el POST
    exitoso y el commit local), la adopta sin volver a crearla — y sin
    siquiera necesitar resolver el cliente SAP (no hay SAPCustomer cargado
    en este test a propósito, para probar que el short-circuit ocurre antes
    de esa consulta).
    """
    factura, orden = await _armar(session)

    monkeypatch.setattr(
        billing.sap_billing, "buscar_factura_existente",
        lambda **kwargs: {"DocEntry": 777, "DocNum": 111},
    )
    llamadas_post = []
    monkeypatch.setattr(
        billing.sap_billing, "create_sap_invoice",
        lambda payload: llamadas_post.append(payload) or _Respuesta(ok=True, data={}),
    )

    resultado = await billing.create_sap_invoice(session, factura, orden)

    assert resultado.status == "COMPLETED"
    assert resultado.doc_entry == 777
    assert resultado.doc_num == 111
    assert llamadas_post == []


async def test_sin_tasa_de_cambio_marca_failed_sin_llegar_a_sap(session, monkeypatch):
    factura, orden = await _armar(session)
    session.add(SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    ))
    await session.commit()

    def _sin_tasa(fecha):
        raise billing.exchange_rates.TasaCambioError(f"No se pudo asegurar la tasa USD de {fecha}")
    monkeypatch.setattr(billing.exchange_rates, "asegurar_tasa_cambio", _sin_tasa)

    llamados_post = []
    monkeypatch.setattr(
        billing.sap_billing, "create_sap_invoice",
        lambda payload: llamados_post.append(payload) or _Respuesta(ok=True, data={}),
    )

    with pytest.raises(billing.TransientError):
        await billing.create_sap_invoice(session, factura, orden)

    assert factura.status == "FAILED"
    assert "Tasa de cambio" in factura.status_message
    assert llamados_post == []


def test_billing_payload_build_respeta_r1_docduedate_igual_a_docdate():
    factura = SAPBilling(
        woo_order_id=1, chunk_index=0, doc_type_code="39", total=1190,
        doc_date=date(2026, 8, 13), internal_notes="x", public_notes="x",
        items=[{"sku": "ML0001", "qty": 1, "price": 1000, "total": 1000, "warehouse_code": "01"}],
    )
    cliente = SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    )

    payload = billing.sap_billing.BillingPayload.build(factura, cliente, order_num=100)
    volcado = payload.model_dump(by_alias=True)

    assert volcado["DocDate"] == volcado["DocDueDate"] == "2026-08-13"


def _factura_y_cliente(purchase_order_code=None):
    factura = SAPBilling(
        woo_order_id=1, chunk_index=0, doc_type_code="39", total=1190,
        doc_date=date(2026, 8, 13), internal_notes="x", public_notes="x",
        purchase_order_code=purchase_order_code,
        items=[{"sku": "ML0001", "qty": 1, "price": 1000, "total": 1000, "warehouse_code": "01"}],
    )
    cliente = SAPCustomer(
        tax_id="12345678-5", code="CN12345678-5", name="Cliente", phone="+56900000000",
        email="c@example.com", contact_name="CONTACTO", ship_code="DESPACHO", bill_code="FISCAL",
    )
    return factura, cliente


def test_billing_payload_con_orden_compra_llena_folio_ref_y_tpo_doc():
    """
    tax_document.orden_compra de BioCommerce (2026-09-04) -- cuando el
    pedido trae ese dato, se manda U_FolioRef/U_TpoDocRef=801/U_FchRef=hoy
    (Chile), distinto de DocDate (que es la fecha de pago, no la de hoy).
    """
    from app.utils.dates import hoy_chile

    factura, cliente = _factura_y_cliente(purchase_order_code="OC-2026-001")

    payload = billing.sap_billing.BillingPayload.build(factura, cliente, order_num=100)
    volcado = payload.model_dump(by_alias=True, exclude_none=True)

    assert volcado["U_FolioRef"] == "OC-2026-001"
    assert volcado["U_TpoDocRef"] == "801"
    assert volcado["U_FchRef"] == hoy_chile().strftime("%Y-%m-%d")
    assert volcado["U_FchRef"] != volcado["DocDate"]  # hoy != fecha de pago (2026-08-13)


def test_billing_payload_sin_orden_compra_no_manda_esos_campos():
    """Si tax_document.orden_compra vino vacío/ausente, los 3 campos se
    omiten del todo -- no se mandan como null/vacío a SAP."""
    factura, cliente = _factura_y_cliente(purchase_order_code=None)

    payload = billing.sap_billing.BillingPayload.build(factura, cliente, order_num=100)
    volcado = payload.model_dump(by_alias=True, exclude_none=True)

    assert "U_FolioRef" not in volcado
    assert "U_TpoDocRef" not in volcado
    assert "U_FchRef" not in volcado
