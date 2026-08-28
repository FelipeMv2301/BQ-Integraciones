"""Utilidad de fecha/hora compartida — evita duplicar la misma lógica en
cada modelo que necesita un timestamp naive-UTC."""

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    """
    datetime.utcnow() está deprecado (Python 3.12+), pero las columnas de
    timestamp del proyecto son TIMESTAMP WITHOUT TIME ZONE — cambiar a un
    datetime con tzinfo rompería esas columnas. Se genera con tzinfo y se
    descarta, para tener el mismo valor naive-UTC de siempre sin usar la
    función deprecada.
    """
    return datetime.now(UTC).replace(tzinfo=None)
