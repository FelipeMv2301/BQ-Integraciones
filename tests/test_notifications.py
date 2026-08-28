"""Tests de app.pipelines.notifications — con SQLite en memoria, sin red."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.core.config import settings
from app.models.email import Email
from app.models.enums import EmailEventType
from app.models.sap_invoice import SAPInvoice
from app.pipelines import notifications


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.content = b"1"

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json_data


async def _factura(session, **kwargs) -> SAPInvoice:
    defaults = dict(
        doc_entry=1, folio=42742, doc_type_code="33", status="COMPLETED",
        pdf_base64="cGRmLWZha2U=",
    )
    defaults.update(kwargs)
    factura = SAPInvoice(**defaults)
    session.add(factura)
    await session.commit()
    return factura


# ── prepare_email (BQI-51) ──────────────────────────────────────────────────

async def test_prioriza_contact_email_sobre_customer_email(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email="contacto@example.com", customer_email="cliente@example.com")

    email = await notifications.prepare_email(session, factura)

    assert email.status == "PENDING"
    assert email.to == [{"name": "contacto@example.com", "email": "contacto@example.com"}]


async def test_usa_customer_email_si_no_hay_contact_email(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email=None, customer_email="cliente@example.com")

    email = await notifications.prepare_email(session, factura)

    assert email.to == [{"name": "cliente@example.com", "email": "cliente@example.com"}]


async def test_sin_ningun_email_marca_skipped(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email=None, customer_email=None)

    email = await notifications.prepare_email(session, factura)

    assert email.status == "SKIPPED"
    assert "destinatario" in email.status_message.lower()


async def test_email_invalido_marca_skipped(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email="no-es-un-email", customer_email=None)

    email = await notifications.prepare_email(session, factura)

    assert email.status == "SKIPPED"
    assert "inválidas" in email.status_message.lower()


async def test_idempotente_reutiliza_fila_existente(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email="contacto@example.com", customer_email=None)

    primera = await notifications.prepare_email(session, factura)
    segunda = await notifications.prepare_email(session, factura)

    assert primera.id == segunda.id
    filas = (await session.execute(select(Email))).scalars().all()
    assert len(filas) == 1


async def test_incluye_seller_email_en_bcc_si_existe(session, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(
        session, contact_email="contacto@example.com", customer_email=None,
        seller_email="vendedor@bioquimica.cl",
    )

    email = await notifications.prepare_email(session, factura)

    assert {"name": "vendedor@bioquimica.cl", "email": "vendedor@bioquimica.cl"} in email.bcc


async def test_ya_completed_no_recalcula_destinatarios(session, monkeypatch):
    """
    Guard: si la notificación ya se envió (COMPLETED), prepare_email no
    debe tocarla — de lo contrario podría regresarla a PENDING/SKIPPED
    según el estado ACTUAL del pedido, habilitando un reenvío real en el
    siguiente send_email.
    """
    monkeypatch.setattr(settings, "BREVO_INVOICE_BCC", "")
    factura = await _factura(session, contact_email="contacto@example.com", customer_email=None)
    ya_enviado = Email(
        event_type=EmailEventType.CUSTOMER_INVOICE.value, sap_invoice_id=factura.id,
        to=[{"name": "contacto@example.com", "email": "contacto@example.com"}],
        status="COMPLETED", brevo_message_id="ya-enviado-123",
    )
    session.add(ya_enviado)
    await session.commit()

    resultado = await notifications.prepare_email(session, factura)

    assert resultado.id == ya_enviado.id
    assert resultado.status == "COMPLETED"
    assert resultado.brevo_message_id == "ya-enviado-123"


# ── send_email (BQI-52) ─────────────────────────────────────────────────────

async def _email_pendiente(session, factura) -> Email:
    email = Email(
        event_type=EmailEventType.CUSTOMER_INVOICE.value,
        sap_invoice_id=factura.id,
        to=[{"name": "contacto@example.com", "email": "contacto@example.com"}],
    )
    session.add(email)
    await session.commit()
    return email


async def test_envio_exitoso_marca_completed_con_message_id(session, monkeypatch):
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: _FakeResponse(200, {"messageId": "abc123"}),
    )

    resultado = await notifications.send_email(session, email)

    assert resultado.status == "COMPLETED"
    assert resultado.brevo_message_id == "abc123"
    assert resultado.attempts == 1


async def test_ya_completed_no_vuelve_a_llamar_a_brevo(session, monkeypatch):
    """Guard: si ya está COMPLETED, no reenvía el correo real al cliente."""
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    email.status, email.brevo_message_id = "COMPLETED", "ya-enviado-123"
    await session.commit()

    llamados = []
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: llamados.append(payload) or _FakeResponse(200, {"messageId": "otro"}),
    )

    resultado = await notifications.send_email(session, email)

    assert resultado.brevo_message_id == "ya-enviado-123"
    assert llamados == []


async def test_no_produccion_redirige_destinatario_real(session, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    payloads = []
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: (payloads.append(payload), _FakeResponse(200, {"messageId": "abc"}))[1],
    )

    await notifications.send_email(session, email)

    assert payloads[0]["to"] == [{"name": "felipe.morales@bioquimica.cl", "email": "felipe.morales@bioquimica.cl"}]
    assert "bcc" not in payloads[0]
    assert "[PRUEBA]" in payloads[0]["subject"]


async def test_produccion_manda_al_destinatario_real(session, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    payloads = []
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: (payloads.append(payload), _FakeResponse(200, {"messageId": "abc"}))[1],
    )

    await notifications.send_email(session, email)

    assert payloads[0]["to"] == email.to
    assert "subject" not in payloads[0]


async def test_brevo_ok_sin_message_id_marca_failed_y_lanza_transient(session, monkeypatch):
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: _FakeResponse(200, {}),
    )

    with pytest.raises(notifications.TransientError):
        await notifications.send_email(session, email)

    assert email.status == "FAILED"
    assert email.brevo_message_id is None


async def test_brevo_rechaza_marca_failed_y_lanza_transient(session, monkeypatch):
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)
    monkeypatch.setattr(
        notifications.brevo_client, "enviar_correo",
        lambda payload: _FakeResponse(400, {"code": "invalid_parameter", "message": "algo falló"}, text="algo falló"),
    )

    with pytest.raises(notifications.TransientError):
        await notifications.send_email(session, email)

    assert email.status == "FAILED"
    assert "algo falló" in email.status_message


async def test_error_de_red_marca_failed_y_lanza_transient(session, monkeypatch):
    factura = await _factura(session)
    email = await _email_pendiente(session, factura)

    def _lanzar(payload):
        raise ConnectionError("Brevo inalcanzable")
    monkeypatch.setattr(notifications.brevo_client, "enviar_correo", _lanzar)

    with pytest.raises(notifications.TransientError):
        await notifications.send_email(session, email)

    assert email.status == "FAILED"


async def test_factura_inexistente_marca_failed_y_lanza_transient(session):
    email = Email(event_type=EmailEventType.CUSTOMER_INVOICE.value, sap_invoice_id=999999, to=[])
    session.add(email)
    await session.commit()

    with pytest.raises(notifications.TransientError):
        await notifications.send_email(session, email)

    assert email.status == "FAILED"


async def test_evento_no_soportado_lanza_not_implemented(session, monkeypatch):
    factura = await _factura(session)
    email = Email(event_type=EmailEventType.INTERNAL_ALERT.value, sap_invoice_id=factura.id, to=[])
    session.add(email)
    await session.commit()

    with pytest.raises(NotImplementedError):
        await notifications.send_email(session, email)


# ── notify_failure (BQI-53) ──────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        pass


def test_primera_ocurrencia_envia_correo(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(notifications.smtp_client, "enviar_correo", lambda *a, **k: None)

    assert notifications.notify_failure("sap_down", "SAP no responde") is True


def test_notify_failure_usa_smtp_no_brevo(monkeypatch):
    """Regresión: las alertas internas van por SMTP directo, nunca por Brevo."""
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: _FakeRedis())
    llamados = []
    monkeypatch.setattr(
        notifications.smtp_client, "enviar_correo",
        lambda destinatarios, asunto, html: llamados.append((destinatarios, asunto, html)),
    )

    def _brevo_no_debe_llamarse(payload):
        raise AssertionError("notify_failure no debe usar brevo_client")
    monkeypatch.setattr(notifications.brevo_client, "enviar_correo", _brevo_no_debe_llamarse)

    notifications.notify_failure("sap_down", "SAP no responde")

    assert llamados[0][0] == ["felipe.morales@bioquimica.cl"]
    assert "sap_down" in llamados[0][1]
    assert "SAP no responde" in llamados[0][2]


def test_segunda_ocurrencia_en_la_ventana_no_reenvia(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")
    fake = _FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake)
    llamados = []
    monkeypatch.setattr(
        notifications.smtp_client, "enviar_correo",
        lambda *a, **k: llamados.append(a),
    )

    primero = notifications.notify_failure("sap_down", "SAP no responde")
    segundo = notifications.notify_failure("sap_down", "SAP no responde de nuevo")

    assert primero is True
    assert segundo is False
    assert len(llamados) == 1


def test_redis_caido_envia_igual_fail_open(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")

    def _raise(*a, **k):
        raise ConnectionError("Redis inalcanzable")
    monkeypatch.setattr("redis.Redis.from_url", _raise)
    monkeypatch.setattr(notifications.smtp_client, "enviar_correo", lambda *a, **k: None)

    assert notifications.notify_failure("facele_down", "Facele no responde") is True


def test_smtp_falla_devuelve_false(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_EMAILS", "felipe.morales@bioquimica.cl")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: _FakeRedis())

    def _falla(*a, **k):
        raise ConnectionError("SMTP inalcanzable")
    monkeypatch.setattr(notifications.smtp_client, "enviar_correo", _falla)

    assert notifications.notify_failure("sap_down", "SAP no responde") is False


def test_sin_destinatarios_no_envia(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_EMAILS", "")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: _FakeRedis())

    assert notifications.notify_failure("sin_destinatarios", "x") is False
