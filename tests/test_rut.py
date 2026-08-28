"""Tests de app.utils.rut."""

from app.utils.rut import es_rut_valido, normalizar_rut


def test_rut_valido_con_guion():
    assert es_rut_valido("12345678-5") is True


def test_rut_invalido_digito_verificador_incorrecto():
    assert es_rut_valido("12345678-9") is False


def test_rut_vacio_o_none_es_invalido():
    assert es_rut_valido("") is False
    assert es_rut_valido(None) is False
    assert es_rut_valido("   ") is False


def test_rut_con_formato_no_procesable_no_explota():
    """rut_chile.is_valid_rut lanza ValueError con esto — acá debe volver False, no propagar."""
    assert es_rut_valido("no-es-un-rut") is False


def test_normalizar_rut_quita_puntos_y_deja_digito_verificador_en_mayuscula():
    """Caso real confirmado contra SAP en BQI-23: con puntos no encuentra resultados."""
    assert normalizar_rut("70.990.700-k") == "70990700-K"


def test_normalizar_rut_ya_normalizado_no_cambia():
    assert normalizar_rut("70990700-K") == "70990700-K"
