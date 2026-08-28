"""
Cliente SOAP para Facele/Docele — obtención de PDF de facturas/boletas ya
emitidas. Puerto de services/facele/{config,document}.py +
services/facele/schemas/{facele,document}.py de Integrify-Consola.

Sin ambiente de test usable (es un sistema distinto al de producción) — se
verifica directo contra producción con folios ya emitidos (operación de
solo lectura vía SOAP, no crea ni modifica nada).
"""

import base64
import logging

import requests
import xmltodict

from app.core.api_log import make_response_hook
from app.core.config import settings

logger = logging.getLogger(__name__)

_PATH = "DoceleOL_Auth/DocumentosEmitidosService"
_SOAP_ACTION = "http://ws.docele.cl/DocumentosEmitidos/Consultar"

_http = requests.Session()
_http.hooks["response"].append(make_response_hook("Facele"))


def decodificar_pdf(pdf_doble_codificado: str) -> str:
    """
    Decodifica UNA vez el PDF que Facele/Docele envía doblemente
    codificado en base64 (R4, confirmado contra producción con folio real
    — el resultado de un solo decode sigue siendo base64, es lo que se
    guarda/envía como adjunto tal cual, sin decodificar una segunda vez).
    """
    try:
        return base64.b64decode(pdf_doble_codificado).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Error decodificando PDF de Facele/Docele: {exc}") from exc


def _armar_xml_solicitud(rut: str, doc_type_code: int, folio: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:doc="http://ws.docele.cl/DocumentosEmitidos/">
    <soapenv:Header/>
    <soapenv:Body>
        <doc:Obtener>
            <rutContribuyente>{rut}</rutContribuyente>
            <tipoDTE>{doc_type_code}</tipoDTE>
            <folioDTE>{folio}</folioDTE>
            <formato>PDF</formato>
        </doc:Obtener>
    </soapenv:Body>
</soapenv:Envelope>"""


def _parsear_respuesta(xml_texto: str) -> dict:
    """
    Extrae {estado, descripcion, pdf} del XML SOAP de respuesta. estado=1
    es éxito, 0 es error (mismo criterio que FaceleBaseModel del original).
    """
    try:
        parsed = xmltodict.parse(xml_texto)
        body = parsed.get("S:Envelope", {}).get("S:Body", {})

        clave_respuesta = next((k for k in body if "Response" in k), None)
        if not clave_respuesta:
            raise ValueError("No se encontró elemento Response en el XML")

        respuesta = body[clave_respuesta]
        operacion = respuesta.get("respuestaOperacion", {})

        return {
            "estado": int(operacion.get("estado", 0)),
            "descripcion": operacion.get("descripcion"),
            "pdf": respuesta.get("pdf"),
        }
    except Exception as exc:
        logger.warning("Error parseando respuesta de Facele/Docele: %s", exc)
        return {"estado": 0, "descripcion": f"Error parseando respuesta XML: {exc}", "pdf": None}


def obtener_documento(doc_type_code: int, folio: int, rut: str | None = None) -> dict:
    """
    Consulta el PDF de una factura/boleta ya emitida. {estado, descripcion,
    pdf} — estado==1 es éxito; pdf viene en base64 DOBLEMENTE codificado
    si estado==1 (R4 del backlog, confirmado en el original).
    """
    xml_solicitud = _armar_xml_solicitud(
        rut=rut or settings.FACELE_TAXID, doc_type_code=doc_type_code, folio=folio
    )
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": _SOAP_ACTION,
        "facele.user": settings.FACELE_USER,
        "facele.pass": settings.FACELE_PASSWORD,
    }
    url = f"{settings.FACELE_URL.rstrip('/')}/{_PATH}"

    respuesta = _http.post(url, data=xml_solicitud.encode("utf-8"), headers=headers, timeout=30)
    respuesta.raise_for_status()
    return _parsear_respuesta(respuesta.text)