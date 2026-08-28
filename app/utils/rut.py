"""
Validación de RUT chileno.

Envoltorio propio sobre la librería rut_chile: is_valid_rut() de la
librería lanza ValueError con formatos que no puede procesar (en vez de
devolver False) — acá se centraliza ese manejo para que el resto del
proyecto solo tenga que llamar a una función que nunca explota.
"""

from rut_chile import rut_chile


def es_rut_valido(rut: str | None) -> bool:
    """
    True si `rut` es un RUT chileno válido (dígito verificador correcto).
    Acepta con o sin puntos/guion. False para None, vacío, o cualquier
    formato que la librería no pueda procesar.
    """
    if not rut or not rut.strip():
        return False
    try:
        return rut_chile.is_valid_rut(rut.strip())
    except ValueError:
        return False


def normalizar_rut(rut: str) -> str:
    """
    Formato canónico para guardar/buscar en SAP: sin puntos, con guion,
    dígito verificador en mayúscula (ej: "70.990.700-k" -> "70990700-K").
    Confirmado con SAP real (BQI-23): busca por FederalTaxID sin puntos —
    si se manda con puntos, no encuentra nada.

    Asume que `rut` ya pasó por es_rut_valido() — no valida de nuevo, solo
    formatea. Con un RUT inválido, el comportamiento queda a cargo de la
    librería (puede lanzar ValueError).
    """
    return rut_chile.format_capitalized_rut_without_dots(rut.strip())
