# API — BQ-Integraciones

Referencia de los endpoints disponibles hoy. Se actualiza a medida que se agregan nuevos.

**Autenticación:** header `X-API-Key`, requerido en todos los endpoints excepto `/health`. Si
`API_KEY` no está seteada en el `.env` del ambiente, la verificación se omite (desarrollo local
sin key configurada) — no confundir con "protegido".

**Swagger/OpenAPI:** `GET /docs` (interfaz interactiva, probar endpoints desde el navegador)
y `GET /openapi.json` (spec cruda) — vienen gratis de FastAPI, sin configuración extra.

**Base local (dev):** `http://localhost:8000` (uvicorn directo) o `http://localhost:8020`
(docker-compose, mapea 8020→8000).

---

## `GET /health`

Liveness puro — no toca la base de datos. Es lo que usa el `healthcheck` de
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

Lista el historial de reintentos agotados (tabla `failures`, BQI-61) — cada fila es un
evento de "esto se dio por vencido", no un estado actual. Ordenado del más reciente al
más viejo.

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
Hoy la tabla está vacía en la práctica — nada la puebla todavía (ninguna fase agota
reintentos automáticamente sin el orquestador conectado). Existe y funciona, a la espera.

---

## `POST /retry/{tabla}/{entity_id}`

Reintenta **una sola fase** sobre una fila que ya existe, llamando **directo y síncrono**
a la función de pipeline correspondiente (no encola nada — ver nota de diseño abajo).
Responde con el resultado inmediato, no hay que consultar después.

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
por qué — nunca un 500 opaco (la función de pipeline ya deja el motivo grabado antes de fallar).

**Errores:**
- `404` — tabla desconocida, o `entity_id` no existe en esa tabla.
- `409` — la fila ya está `COMPLETED`, no se reintenta (evita duplicar en SAP/reenviar un email real).
- `422` — falta un dato necesario para reintentar (ej. `sap_customers`/`sap_billings` sin un
  `WooOrder` asociado del que sacar los datos).

**Nota de diseño:** esto llama directo a la función Python, no pasa por Celery — las fases
3/5/6/7 todavía no tienen tarea propia (el orquestador real no está conectado). Cuando se
conecte, este endpoint puede seguir existiendo igual para reintentos manuales puntuales.

---

## `POST /pipeline/sync-order/{code}`

Sincroniza **un pedido puntual** de WooCommerce hasta SAP, de punta a punta, para pruebas
dirigidas — no reemplaza el polling automático (que todavía no está conectado).

**`code`** — el **ID interno** de WooCommerce del pedido (`id` en la API de Woo, no el
número de pedido visible al cliente — ver nota abajo).

**Qué hace, en orden:**
1. Si el pedido no está en `woo_orders` todavía, lo trae de WooCommerce por ID puntual (no
   el polling de todos los `processing`).
2. `resolve_customer()` — crea/actualiza el cliente en SAP.
3. `prepare_billing()` — trocea en uno o más `SAPBilling` (lotes de 21 ítems).
4. `create_sap_invoice()` por cada chunk — con **auto-carga de tasa de cambio** si SAP no
   la tiene para la fecha exacta del pedido (`asegurar_tasa_cambio`, trae el valor real de
   mindicador.cl y lo carga en SAP solo, sin intervención manual).

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
Nunca devuelve un 500 sin manejar — cualquier falla queda descrita en `error` (a nivel
pedido) o dentro de la `factura` puntual que falló.

**Nota — `code` vs número de pedido visible:** WooCommerce tiene dos identificadores
distintos para un mismo pedido: `id` (interno, el que usa este endpoint, comparte
secuencia con todos los posts de WordPress) y `number` (el que ve el cliente/admin, ej.
`#24683`). Si tenés el número visible y no el ID interno, se puede consultar en
WooCommerce admin o en la tabla `woo_orders` (columna `reference`).

---

## `GET /pipeline/status` · `POST /pipeline/enable` · `POST /pipeline/disable`

Interruptor del procesamiento automático (Beat) — Redis (`pipeline:enabled`), no `.env`, para
poder pausar/reanudar sin redeploy. **Por defecto apagado** (y si Redis no responde, se
asume apagado — nunca "procesar a ciegas"). Cuando está apagado, `task_poll_woo_orders`/
`task_poll_sap_invoices` devuelven `{"skipped": "disabled"}` sin tocar nada — pero Beat
sigue vivo y el heartbeat de Healthchecks sigue pingeando igual (son cosas distintas).

Estos 3 endpoints funcionan siempre, esté prendido o apagado — es el control manual.

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
| `ENVIRONMENT=production` | `send_email()` manda al destinatario real de la fila, tal cual |
| `API_KEY` | declarada pero sin middleware que la use todavía (BQI-64 pendiente) |
