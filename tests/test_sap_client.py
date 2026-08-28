"""
Tests de app.services.sap.client.
"""

import requests

from app.services.sap import client, session


class _Respuesta:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")


def test_url_odata_preserva_caracteres_especiales():
    url = client._url_odata("Orders", {"$filter": "startswith(Name,'a')"})
    assert "$filter=" in url
    assert "startswith(Name,'a')" in url
    assert "%24filter" not in url


def test_url_odata_sin_params():
    url = client._url_odata("Orders", None)
    assert url.endswith("/Orders")
    assert "?" not in url


def test_solicitar_reintenta_una_vez_en_401(monkeypatch):
    llamadas = {"cookies": 0, "invalidar": 0, "requests": 0}

    def _cookies():
        llamadas["cookies"] += 1
        return {"B1SESSION": f"sesion-{llamadas['cookies']}"}

    def _invalidar(b1session):
        llamadas["invalidar"] += 1

    respuestas = [_Respuesta(status_code=401), _Respuesta(status_code=200, data={"value": []})]

    def _request(metodo, url, **kwargs):
        llamadas["requests"] += 1
        return respuestas.pop(0)

    monkeypatch.setattr(session, "obtener_cookies", _cookies)
    monkeypatch.setattr(session, "invalidar", _invalidar)
    monkeypatch.setattr(client._http, "request", _request)

    respuesta = client.solicitar("GET", "Orders")

    assert respuesta.status_code == 200
    assert llamadas["cookies"] == 2  # una antes del 401, una después de invalidar
    assert llamadas["invalidar"] == 1
    assert llamadas["requests"] == 2


def test_solicitar_no_reintenta_dos_veces_si_sigue_401(monkeypatch):
    llamadas = {"requests": 0}

    monkeypatch.setattr(session, "obtener_cookies", lambda: {"B1SESSION": "x"})
    monkeypatch.setattr(session, "invalidar", lambda b1session: None)

    def _request(metodo, url, **kwargs):
        llamadas["requests"] += 1
        return _Respuesta(status_code=401)

    monkeypatch.setattr(client._http, "request", _request)

    respuesta = client.solicitar("GET", "Orders")

    assert respuesta.status_code == 401
    assert llamadas["requests"] == 2  # intento original + único reintento, nunca un tercero


def test_obtener_todas_las_paginas_junta_resultados_de_varias_paginas(monkeypatch):
    paginas = [
        _Respuesta(data={"value": [{"CardCode": "A"}, {"CardCode": "B"}]}),
        _Respuesta(data={"value": [{"CardCode": "C"}]}),
    ]

    def _solicitar_falso(metodo, endpoint, params=None, headers=None):
        return paginas.pop(0)

    monkeypatch.setattr(client, "solicitar", _solicitar_falso)

    items = client.obtener_todas_las_paginas("BusinessPartners", items_por_pagina=2)

    assert [i["CardCode"] for i in items] == ["A", "B", "C"]
