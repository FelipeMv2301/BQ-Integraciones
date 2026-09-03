# API — BQ-Integraciones

Referencia de los endpoints disponibles hoy. Se actualiza a medida que se agregan nuevos.

**Autenticación:** header `X-API-Key`, **exigido de verdad** en todos los endpoints excepto
`/health`, `/docs`, `/redoc` y `/openapi.json`. Si `API_KEY` no está seteada en el `.env` del
ambiente, la verificación se omite (desarrollo local sin key configurada) — no confundir con
"protegido".

```
curl -H "X-API-Key: <valor>" https://bq-integraciones-dev.bioquimica.cl/status
```

**Swagger/OpenAPI:** `GET /docs` (interfaz interactiva) y `GET /openapi.json` (spec cruda) —
gratis de FastAPI, sin key. Ojo: ninguna ruta declara `response_model` de Pydantic, así que
Swagger muestra el tipo de respuesta genérico ("object"/"array"), no los campos reales — para
eso está este documento, con ejemplos reales de cada respuesta.

**Bases:**
- Local (dev, sin docker): `http://localhost:8000`
- Local (docker-compose): `http://localhost:8020` (mapea a 8020→8000)
- Pública (ambiente `desarrollo`): `https://bq-integraciones-dev.bioquimica.cl`

---

## `GET /health`

Liveness puro — no toca la base de datos, no pide API Key. Es lo que usa el `healthcheck` de
`docker-compose.yml` cada 30s para decidir si reinicia el contenedor `api`.

**Respuesta:**
```json
{"status": "ok"}
```

---

## `GET /status`

Conteo de filas por `status` en cada tabla de trabajo — para saber de un vistazo cómo viene
el pipeline (cuántos pedidos `PENDING`, cuántas facturas `FAILED`, etc.). Refleja el estado
real de Postgres en el momento de la consulta.

**Respuesta:**
```json
{
  "woo_orders":    {"PENDING": 42, "COMPLETED": 3},
  "sap_customers": {"COMPLETED": 1},
  "sap_billings":  {"PENDING": 2, "COMPLETED": 3},
  "sap_invoices":  {"COMPLETED": 3},
  "emails":        {"COMPLETED": 1}
}
```
Solo aparecen las 5 tablas con `SyncStatusMixin` (tienen columna `status`) — los catálogos
(`municipalities`, etc.) no.

---

## `GET /failures`

Lista el historial de reintentos agotados (tabla `failures`) — cada fila es un evento de
"esto se dio por vencido tras N intentos", no un estado actual. Ordenado del más reciente al
más viejo. Se llena sola cuando `procesar_pedidos_pendientes`/`procesar_facturas_pendientes`
(Beat) o `/retry` agotan `*_MAX_ATTEMPTS` (ver `app/pipelines/failure_tracking.py`).

**Respuesta:**
```json
[
  {
    "id": 1,
    "entity_type": "SAPBilling",
    "entity_id": 42,
    "stage": "create_sap_invoice",
    "error_message": "SAP 400: ...",
    "attempts": 10,
    "occurred_at": "2026-08-18T15:00:00",
    "notified": false
  }
]
```

---

## `POST /retry/{tabla}/{entity_id}`

Reintenta **una sola fase** sobre una fila que ya existe, llamando **directo y síncrono**
a la función de pipeline correspondiente (no encola nada, responde con el resultado inmediato).

**`tabla`** — una de: `woo_orders` · `sap_customers` · `sap_billings` · `sap_invoices` · `emails`

| `tabla` | Qué reintenta |
|---|---|
| `woo_orders` | `prepare_billing()` — trocea el pedido en facturación |
| `sap_customers` | `resolve_customer()` — reconstruye los datos desde el `WooOrder` más reciente con el mismo RUT (`SAPCustomer` no guarda a qué pedido pertenece) |
| `sap_billings` | `create_sap_invoice()` — crea la factura en SAP (con auto-carga de tasa de cambio) |
| `sap_invoices` | `fetch_pdf()` — trae el PDF de Facele/Docele |
| `emails` | `send_email()` — reenvía el correo |

**Respuesta (200):**
```json
{"tabla": "sap_billings", "id": 42, "status": "COMPLETED", "status_message": null}
```
Si la fase falla, igual responde 200 con `status: "FAILED"` y el `status_message` explicando
por qué — nunca un 500 opaco. Si agota `*_MAX_ATTEMPTS` en este mismo intento, escala a
`EXHAUSTED` y queda una fila nueva en `/failures`.

**Errores:**
- `404` — tabla desconocida, o `entity_id` no existe en esa tabla.
- `409` — la fila ya está `COMPLETED`, no se reintenta (evita duplicar en SAP/reenviar un email real).
- `422` — falta un dato necesario para reintentar (ej. `sap_customers`/`sap_billings` sin un
  `WooOrder` asociado del que sacar los datos).

---

## `POST /pipeline/sync-order/{code}`

Sincroniza **un pedido puntual** hasta SAP, de punta a punta, para pruebas dirigidas — no
reemplaza el polling automático de Beat. Pedido vía **BioCommerce PRO**
(`GET /wp-json/bio-commerce/v1/orders/{code}/payload`, único origen del proyecto desde
2026-09-03 — se retiró el path nativo de WooCommerce, que leía `meta_data` a mano y rompía cada
vez que el checkout cambiaba de mecanismo).

**`code`** — el **ID interno** del pedido (`order.id` en el payload de BioCommerce, **no** el
número de pedido visible al cliente).

**Qué hace, en orden:**
1. Si el pedido no está en `woo_orders` todavía, lo trae de BioCommerce por ID puntual — ya
   viene con RUT, tipo de documento, giro y código de comuna resueltos, sin escanear `meta_data`
   a mano.
2. `resolve_customer()` — crea/actualiza el cliente en SAP.
3. `prepare_billing()` — trocea en uno o más `SAPBilling` (lotes de 21 ítems).
4. `create_sap_invoice()` por cada chunk — con auto-carga de tasa de cambio si hace falta.

**Respuesta (200) — camino feliz:**
```json
{
  "code": 27469,
  "cliente": "CN76230007-9",
  "facturas": [
    {"chunk_index": 0, "doc_entry": 103966, "doc_num": 30373, "status": "COMPLETED"}
  ],
  "error": null
}
```

**Respuesta — una fase previa falló (no llega a intentar facturar):**
```json
{"code": 12345, "cliente": null, "facturas": [], "error": "resolve_customer: RUT no válido: '...'"}
```

**Respuesta — un chunk específico falló (los demás sí se procesan, I2):**
```json
{
  "code": 27385,
  "cliente": "CN20195519-K",
  "facturas": [
    {"chunk_index": 0, "status": "FAILED", "error": "SAP 400: ..."},
    {"chunk_index": 1, "doc_entry": 103970, "doc_num": 7420, "status": "COMPLETED"}
  ],
  "error": null
}
```
Nunca devuelve un 500 sin manejar — cualquier falla queda descrita en `error` o dentro de la
`factura` puntual que falló.

**Probado en vivo** contra SAP TEST: pedido `9237` (bioquimica.devwebs.cl) llegó `COMPLETED`
(`DocEntry 104019`, `DocNum 30402`) de punta a punta.

---

## `POST /pipeline/sync-invoice/{doc_entry}`

Equivalente a los dos anteriores, pero para la otra punta del pipeline: **folio → PDF (Facele)
→ correo (Brevo)**, para UNA factura puntual. No tiene endpoint de "solo Facele" o "solo
Brevo" sueltos — este es el que las cubre a ambas, de punta a punta, en un solo llamado.

**`doc_entry`** — el `DocEntry` de la factura en SAP (el mismo que devuelve `sync-order` al
crearla).

**Qué hace, en orden:**
1. Si ya existe la fila en `sap_invoices` para ese `doc_entry`, la reutiliza tal cual.
2. Si no, busca el `SAPBilling` con ese `doc_entry` y le consulta a SAP si ya le asignó folio
   (`FolioNumber ne null`) — si SAP todavía no lo asignó, no es un error, es "esperar":
   responde con `error` explicándolo, para reintentar más tarde.
3. Con folio confirmado, crea la fila en `sap_invoices` (mismo criterio que `poll_sap_invoices`,
   la espera automática de Beat).
4. `fetch_pdf()` (Facele/Docele) si el PDF no está listo todavía.
5. `prepare_email()` + `send_email()` (Brevo) si el correo no se mandó todavía.

**Respuesta (200) — camino feliz:**
```json
{"sap_invoice_id": 3, "status": "COMPLETED", "error": null}
```

**Respuesta — SAP todavía no asignó folio:**
```json
{"doc_entry": 103970, "sap_invoice_id": null, "status": null, "error": "SAP todavía no le asignó folio a doc_entry=103970 — esperar y reintentar"}
```

**Respuesta — no existe ese `doc_entry` en `sap_billings`:**
```json
{"doc_entry": 999999, "sap_invoice_id": null, "status": null, "error": "No hay SAPBilling con doc_entry=999999"}
```

---

## `GET /pipeline/status` · `POST /pipeline/enable` · `POST /pipeline/disable`

Interruptor del procesamiento automático (Beat) — vive en Redis (`pipeline:enabled`), no en
`.env`, para poder pausar/reanudar sin redeploy. **Por defecto apagado**, y si Redis no
responde se asume apagado (fail-closed — nunca "procesar a ciegas"). Cuando está apagado,
`task_poll_woo_orders`/`task_poll_sap_invoices` devuelven `{"skipped": "disabled"}` sin tocar
nada — pero Beat sigue vivo y el heartbeat de Healthchecks sigue pingeando igual (son cosas
distintas). Estos 3 endpoints funcionan siempre, esté prendido o apagado.

```
GET  /pipeline/status   -> {"enabled": false}
POST /pipeline/enable   -> {"enabled": true}
POST /pipeline/disable  -> {"enabled": false}
```

---

## Variables de entorno relevantes para pruebas

| Variable | Efecto |
|---|---|
| `ENVIRONMENT=development` (default) | `send_email()` redirige el destinatario real a `ALERT_EMAILS` (vos) con `[PRUEBA]` en el asunto — ningún correo de prueba llega a un cliente real |
| `ENVIRONMENT=production` | `send_email()` manda al destinatario real de la fila, tal cual. Si además `API_KEY` está vacío, el proceso **ni arranca** (falla rápido en vez de exponer la API sin auth) |
| `API_KEY` | header `X-API-Key` exigido en todos los endpoints salvo `/health`/`/docs`/`/redoc`/`/openapi.json`. Vacío = sin protección (dev local) |
