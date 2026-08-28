"""
Sanitización de texto a las reglas de SAP Business One. Puerto exacto de
utils/sap.py::sanitize_sap_text de Integrify-Consola.
"""

import re

from unidecode import unidecode

# Caracteres especiales permitidos en SAP, además de letras/números/espacios
_CARACTERES_ESPECIALES_PERMITIDOS = {",", ".", "+", "@"}
_PATRON_NO_PERMITIDO = rf"[^a-zA-Z0-9{''.join(map(re.escape, _CARACTERES_ESPECIALES_PERMITIDOS))}\s]"


def sanitizar_texto_sap(texto: str | None, max_length: int | None = None) -> str:
    """
    Limpia un texto para que SAP lo acepte: transliteral acentos/ñ a ASCII
    (unidecode), elimina cualquier carácter que no sea letra/número/espacio/
    uno de `,.+@`, colapsa espacios múltiples, y recorta a max_length si se
    especifica.
    """
    if not texto:
        return ""

    texto_normalizado = unidecode(texto)
    texto_limpio = re.sub(_PATRON_NO_PERMITIDO, "", texto_normalizado)
    texto_limpio = re.sub(r"\s+", " ", texto_limpio).strip()

    if max_length:
        texto_limpio = texto_limpio[:max_length]

    return texto_limpio