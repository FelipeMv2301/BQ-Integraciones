from enum import Enum


class SyncStatus(str, Enum):
    """
    PENDING     -> creado, todavía no procesado
    IN_PROGRESS -> un worker lo está procesando ahora
    COMPLETED   -> confirmado por el sistema externo correspondiente
    FAILED      -> falló, reintenta mientras no agote el máximo de intentos
    SKIPPED     -> no corresponde procesar (dato inválido, deshabilitado)
    EXHAUSTED   -> agotó los reintentos — requiere revisión manual (tabla failures)
    """
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    EXHAUSTED = "EXHAUSTED"


class EmailEventType(str, Enum):
    """
    CUSTOMER_INVOICE -> factura/boleta al cliente, con PDF adjunto
    INTERNAL_ALERT    -> alerta interna de fallo definitivo, sin adjunto
    """
    CUSTOMER_INVOICE = "CUSTOMER_INVOICE"
    INTERNAL_ALERT = "INTERNAL_ALERT"