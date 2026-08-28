"""Tests de app.services.facele.client — sin red."""

import base64

import pytest

from app.services.facele import client


def test_decodificar_pdf_una_vez_da_base64_valido():
    pdf_real = b"%PDF-1.4 contenido de prueba"
    codificado_una_vez = base64.b64encode(pdf_real).decode("utf-8")
    codificado_dos_veces = base64.b64encode(codificado_una_vez.encode("utf-8")).decode("utf-8")

    resultado = client.decodificar_pdf(codificado_dos_veces)

    assert resultado == codificado_una_vez
    assert base64.b64decode(resultado) == pdf_real


def test_decodificar_pdf_con_basura_lanza_value_error():
    with pytest.raises(ValueError):
        client.decodificar_pdf("no-es-base64-!!!")


def test_armar_xml_solicitud_incluye_los_datos():
    xml = client._armar_xml_solicitud(rut="76563320-6", doc_type_code=33, folio=42742)
    assert "<rutContribuyente>76563320-6</rutContribuyente>" in xml
    assert "<tipoDTE>33</tipoDTE>" in xml
    assert "<folioDTE>42742</folioDTE>" in xml


def test_parsear_respuesta_exitosa():
    xml = """<?xml version="1.0"?>
    <S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/">
      <S:Body>
        <ns2:ObtenerResponse>
          <respuestaOperacion><estado>1</estado><descripcion>Proceso OK</descripcion></respuestaOperacion>
          <pdf>QkFTRTY0RkFMU08=</pdf>
        </ns2:ObtenerResponse>
      </S:Body>
    </S:Envelope>"""

    resultado = client._parsear_respuesta(xml)

    assert resultado["estado"] == 1
    assert resultado["descripcion"] == "Proceso OK"
    assert resultado["pdf"] == "QkFTRTY0RkFMU08="


def test_parsear_respuesta_con_error_de_negocio():
    xml = """<?xml version="1.0"?>
    <S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/">
      <S:Body>
        <ns2:ObtenerResponse>
          <respuestaOperacion><estado>0</estado><descripcion>Folio no encontrado</descripcion></respuestaOperacion>
        </ns2:ObtenerResponse>
      </S:Body>
    </S:Envelope>"""

    resultado = client._parsear_respuesta(xml)

    assert resultado["estado"] == 0
    assert resultado["descripcion"] == "Folio no encontrado"
    assert resultado["pdf"] is None


def test_parsear_respuesta_xml_invalido_no_explota():
    resultado = client._parsear_respuesta("esto no es XML")
    assert resultado["estado"] == 0
    assert "Error parseando" in resultado["descripcion"]


def test_obtener_documento_manda_headers_y_url_correctos(monkeypatch):
    llamadas = {}

    class _Respuesta:
        text = """<S:Envelope xmlns:S="x"><S:Body><ns2:ObtenerResponse>
                    <respuestaOperacion><estado>1</estado><descripcion>OK</descripcion></respuestaOperacion>
                    <pdf>QUJD</pdf>
                  </ns2:ObtenerResponse></S:Body></S:Envelope>"""

        def raise_for_status(self):
            pass

    def _post_falso(url, data=None, headers=None, timeout=None):
        llamadas["url"] = url
        llamadas["headers"] = headers
        llamadas["data"] = data
        return _Respuesta()

    monkeypatch.setattr(client._http, "post", _post_falso)

    resultado = client.obtener_documento(doc_type_code=33, folio=42742, rut="76563320-6")

    assert resultado["estado"] == 1
    assert llamadas["headers"]["facele.user"] == client.settings.FACELE_USER
    assert llamadas["headers"]["SOAPAction"] == client._SOAP_ACTION
    assert "76563320-6" in llamadas["data"].decode("utf-8")
