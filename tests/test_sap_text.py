"""Tests de app.utils.sap_text."""

from app.utils.sap_text import sanitizar_texto_sap


def test_transliteral_acentos_y_enie():
    assert sanitizar_texto_sap("María Ñuñoa") == "Maria Nunoa"


def test_elimina_caracteres_no_permitidos():
    assert sanitizar_texto_sap("Depto. (Ventas) #123") == "Depto. Ventas 123"


def test_conserva_caracteres_especiales_permitidos():
    assert sanitizar_texto_sap("Juan, Perez S.A. + Cia @ Ventas") == "Juan, Perez S.A. + Cia @ Ventas"


def test_colapsa_espacios_multiples():
    assert sanitizar_texto_sap("Hola     Mundo") == "Hola Mundo"


def test_recorta_a_max_length():
    assert sanitizar_texto_sap("Hola Mundo", max_length=4) == "Hola"


def test_vacio_o_none_devuelve_string_vacio():
    assert sanitizar_texto_sap("") == ""
    assert sanitizar_texto_sap(None) == ""


def test_sin_max_length_no_recorta():
    texto = "Un texto bastante largo que no tiene por que recortarse"
    assert sanitizar_texto_sap(texto) == texto
