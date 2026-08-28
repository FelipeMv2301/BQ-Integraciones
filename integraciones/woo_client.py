from django.conf import settings
import requests

"""Este archivo se encarga de almacenar todas las funciones con las que se llama a WooCommerce"""

#Trae muchos datos a partir de parametros que pueden o no mandarse. De querer añadir más, se puede a través de la variable params
def paginar_woo(status, pagina, after=None, before=None):
    #Traer la url
    base_url = settings.WOO_BASE_URL.rstrip("/")

    #Traer key y secret
    auth = (settings.WOO_CONSUMER_KEY, settings.WOO_CONSUMER_SECRET)

    respuesta = requests.get(
        f"{base_url}/wp-json/wc/v3/orders",
        params={
            "status": status, 
            "per_page": 50, 
            "page": pagina,
            "after": after,
            "before": before,
        },
        auth=auth,
        timeout=60,
    )
    respuesta.raise_for_status()
    return respuesta.json()

#Llama un listado de pedidos por su estado (processing)
def obtener_pedidos_woo(status, after=None, before=None):
    resultados = []
    pagina = 1

    while True:
        lote = paginar_woo(status, pagina, after=after, before=before)
        if not lote:
            break
        resultados.extend(lote)
        pagina += 1
    return resultados

#Obtiene pedido individual a partir del número de pedido
def obtener_un_pedido_woo(numero_pedido):
    for status in ("processing", "completed"):
        pagina = 1
        while True:
            lote = paginar_woo(status, pagina)
            if not lote:
                break
            for pedido in lote:
                if str(pedido.get("number")) == str(numero_pedido):
                    return pedido
            pagina += 1
    return None

def obtener_mapa_comunas():
    base_url = settings.WOO_BASE_URL.rstrip("/")
    auth = (settings.WOO_CONSUMER_KEY, settings.WOO_CONSUMER_SECRET)

    respuesta = requests.get(f"{base_url}/wp-json/wc/v3/data/countries", auth=auth, timeout=60)
    respuesta.raise_for_status()

    paises = respuesta.json()
    chile = next((pais for pais in paises if pais.get("code") == "CL"), None)
    if not chile:
        return {}

    return {estado["code"]: estado["name"] for estado in chile.get("states", [])}
