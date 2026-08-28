# BQ-Integraciones — pipeline Woo → SAP → folio → Facele/Docele → Brevo

## Contexto

Bioquimica.cl opera hoy este flujo dentro de un monolito Django legado
("Integrify-Consola", `C:\Users\920562\Desktop\Integrify-consola`): toma
pedidos de WooCommerce, los sube a SAP Business One (Service Layer), espera a
que SAP asigne el folio del DTE, obtiene el PDF de la boleta/factura desde un
backend SOAP (el código lo llama "Facele", en realidad un wrapper sobre el
proveedor Docele/SII), y envía ese PDF por correo vía Brevo. Funciona pero es
pesado (Django + cron + bash) y cada etapa reintenta "a mano" con
`for/try/except/continue`.

El objetivo es reconstruir esa parte específica como proyecto nuevo e
independiente, aquí (`BQ-Integraciones`, hoy casi vacío — no está productivo),
más liviano, con manejo de errores explícito (ningún fallo individual detiene
el resto del pipeline, pero tampoco se dispara más carga de la que
SAP/Facele/Brevo toleran), desplegable en Railway o en servidor local.

**Hallazgo que cambió el plan inicial**: al buscar dónde viven los datos de
producto que este pipeline necesita (SKU↔bodega SAP para las líneas de
facturación), aparecieron dos proyectos hermanos que **ya resuelven** partes
del problema con un patrón maduro y en producción:

- **`Stock-Service`** (`C:\Users\920562\Documents\proyectos\Stock-Service`,
  desplegado en `stock-sap-bq-production.up.railway.app`) — sincroniza
  catálogo SAP↔WooCommerce. Expone `GET /stock/products/{sku}` (trazabilidad
  por SKU, incluye `sync_warehouse`) y `GET /stock/catalog` (precio neto,
  stock por bodega, dimensiones). **Esto reemplaza por completo la idea
  original de construir nuestro propio `pipeline/products.py`+tabla
  `products`** — se consume la API en vivo, con caché corta en Redis, en vez
  de mantener un espejo propio.
- Su stack (FastAPI + SQLModel + Celery + Beat + Redis + Alembic + pytest) es
  exactamente el nivel de "ligero pero con manejo de errores" que se pidió, ya
  probado en Railway contra los mismos sistemas (Token-SAP-BQ, WooCommerce)
  que este proyecto necesita. Se adopta el mismo stack y se **porta código ya
  escrito y testeado** (`app/services/sap/session.py`,
  `app/services/sap/client.py`, `app/services/woocommerce/client.py`) en vez
  de extender el cliente más primitivo de `integraciones/sap_client.py` (que
  no soporta body JSON ni cachea sesión en Redis).
- Se investigó también `gestorBQ` (`Documents/proyectos/gestorBQ`) porque el
  BACKLOG.md de Stock-Service menciona un tal "Integraciones-BQ" como
  referencia de arquitectura — resultó ser un portal Django de logística
  (despachos a courier, cotizaciones) **sin relación de dominio** con este
  pipeline; es solo el origen de los dos archivos de ejemplo
  (`integraciones/sap_client.py`/`woo_client.py`) que ya estaban en esta
  carpeta. Confirmado con el usuario: no está productivo y no se solapa —
  se descarta como dependencia.

Pedidos explícitos adicionales del usuario para este plan: (1) consumir el
catálogo desde Stock-Service en vez de sincronizarlo, (2) un **backlog
completo** para ir marcando avance — se construye con el mismo template que
`Stock-Service/BACKLOG.md` (documento ya validado en este equipo), (3)
**tests** que cubran los objetivos de la solución — se replica la estrategia
de `Stock-Service/tests/` (pytest + fixtures + tabla de invariantes con test
de cobertura dedicado).

**Decisiones ya tomadas con el usuario** (no reabrir):
- Orquestación: cola de tareas para aislar errores por ítem y dar
  backpressure (ahora concretada como Celery+Redis, igual que Stock-Service,
  en vez de RQ — mismo concepto, se adopta la herramienta ya probada).
- Intake del pedido: polling programado a WooCommerce (no webhook).
- Cliente SAP: **replicar completo** — crear/actualizar el Business Partner
  en SAP (RUT, comuna, direcciones), no asumir que el cliente ya existe.
- Catálogo de productos: **consumir Stock-Service**, no sincronizar propio.

## Arquitectura y estructura de carpetas

Mismo esqueleto que `Stock-Service` (referencia directa, mismas versiones de
dependencias en `pyproject.toml`: fastapi, sqlmodel, asyncpg, alembic,
celery[redis], requests, pydantic-settings, sentry-sdk; dev: pytest,
pytest-asyncio, pytest-mock, httpx, ruff):

```
BQ-Integraciones/
├── app/
│   ├── api/routes/{orders,billing,invoices,failures}.py   # FastAPI, disparo manual + consulta
│   ├── core/{config,database,logging,sentry,api_log}.py   # puerto directo de Stock-Service/app/core
│   ├── models/
│   │   ├── woo_order.py            # snapshot del pedido (cabecera+items+direcciones en JSON)
│   │   ├── sap_customer.py         # Business Partner resuelto/creado en SAP
│   │   ├── sap_billing.py          # facturación creada en SAP (por chunk)
│   │   ├── sap_invoice.py          # factura/boleta con folio + PDF
│   │   ├── email.py                # notificación (cliente o alerta interna)
│   │   ├── failure.py              # tabla de fallos definitivos, visibilidad
│   │   ├── sync_run.py             # una fila por corrida de cada fase (igual a Stock-Service)
│   │   └── reference_data.py       # Municipality, Industry, DeliveryMethod, BillDocumentType
│   ├── pipelines/
│   │   ├── customers.py            # resolve_customer() — puerto de app/customers de Integrify-Consola
│   │   ├── billing.py              # prepare_billing(), create_sap_invoice()
│   │   ├── invoices.py             # poll_sap_invoices()
│   │   ├── documents.py            # fetch_pdf()
│   │   └── notifications.py        # prepare_email(), send_email(), notify_failure()
│   ├── services/
│   │   ├── sap/{session,client}.py         # PORTADO casi verbatim de Stock-Service (I10)
│   │   ├── woocommerce/client.py           # PORTADO de Stock-Service, adaptado a /orders
│   │   ├── facele/client.py                # NUEVO — SOAP Docele, puerto de services/facele/document.py
│   │   ├── brevo/client.py                 # NUEVO — REST Brevo, puerto de services/brevo/email.py
│   │   └── stockservice/client.py          # NUEVO — GET /stock/products/{sku}, caché Redis corta
│   └── tasks/
│       ├── celery_app.py           # Celery + Beat schedule (puerto de Stock-Service)
│       ├── scheduled.py            # poll_woo_orders (5 min), poll_sap_invoices (10 min)
│       ├── locks.py                # PORTADO — pipeline_lock() Redis SET NX EX
│       └── heartbeat.py            # PORTADO — Healthchecks.io
├── alembic/ · tests/ · docs/RUNBOOK.md
├── docker-compose.yml · Dockerfile · pyproject.toml
├── railway.json (api) · railway.worker.json · railway.beat.json
├── BACKLOG.md
└── .env.example
```

Los dos archivos de ejemplo actuales (`integraciones/sap_client.py`,
`woo_client.py`) quedan como referencia histórica — el código nuevo va en
`app/services/*` siguiendo el patrón ya probado de Stock-Service, no
extendiendo esos dos archivos.

## Flujo del pipeline

```
FASE 1  resolve_customer   Woo → SAP        RUT válido, comuna mapeada, BP creado/actualizado
FASE 2  prepare_billing     DB → DB          trocea ítems (lotes de 21) + ítem de envío, resuelve SKU/bodega vía Stock-Service
FASE 3  create_sap_invoice  DB → SAP         POST /Invoices — crea el documento (aún sin folio)
FASE 4  poll_sap_invoices   SAP → DB         filtro FolioNumber ne null — la espera del folio ES este polling
FASE 5  fetch_pdf           Docele → DB      SOAP, decodifica base64 doble
FASE 6  prepare_email       DB → DB          valida destinatarios, construye payload
FASE 7  send_email          DB → Brevo       adjunta PDF, envía
```

Cada fase = una tarea Celery + un `SyncRun` (igual a Stock-Service §4). La
fase 4 corre en su propio ciclo de Beat, independiente de la 1-3 — un pedido
que falla en fase 3 no bloquea el polling de folio de otros (mismo
desacoplamiento que `sync_sap_billing.sh`/`sync_sap_invoices.sh` en el
original).

## Modelo de datos (SQLModel + Alembic)

Columnas de estado comunes (mixin `SyncStatusMixin`, análogo a
`SyncMixin`/`LoadMixin` del original): `status`
(`PENDING|IN_PROGRESS|COMPLETED|FAILED|SKIPPED|EXHAUSTED`),
`status_message`, `attempts`, `last_attempt_at`, `created_at`/`updated_at`.

- **`woo_orders`**: `code` (Woo id, único), `reference`, `paid_at`, `total`,
  `shipping`, `delivery_method_code`, `bill_doc_type_code`,
  `customer_tax_id`, `billing_address`/`shipping_address` (JSON), `items`
  (JSON, snapshot inmutable) + estado.
- **`sap_customers`**: `tax_id` (único), `code`, `contact_code`,
  `contact_name`, `bill_code`/`bill_row`, `ship_code`/`ship_row`,
  `business_activity`, `exists` (bool) + estado (máx. 10 intentos).
- **`sap_billings`**: `woo_order_id` FK, `chunk_index`
  (único con `woo_order_id`), `doc_type_code`, `total`, `doc_date`,
  `items` (JSON ya resuelto: sku/qty/price/warehouse), `doc_entry`
  (nullable), `doc_num` + estado (máx. 10).
- **`sap_invoices`**: `sap_billing_id` FK nullable, `doc_entry` (único),
  `folio`, `folio_prefix`, `customer_email`/`contact_email`/`seller_email`,
  `pdf_base64` + estado (máx. 5).
- **`emails`**: `sap_invoice_id`/`woo_order_id` FK nullable, `event_type`
  (`CUSTOMER_INVOICE|INTERNAL_ALERT`), `to`/`bcc` (JSON), `brevo_message_id`
  + estado (máx. 3).
- **`failures`**: `entity_type`, `entity_id`, `stage`, `error_message`,
  `attempts`, `occurred_at`, `notified` — igual rol que en Stock-Service:
  visibilidad central de todo lo que agotó reintentos.
- **`sync_runs`**: `pipeline` (`RESOLVE_CUSTOMER|BILLING|POLL_INVOICES|...`),
  `triggered_by`, `status`, tiempos, contadores — mismo modelo que
  `Stock-Service/app/models/sync_run.py`, reusar tal cual.
- **`municipalities`/`industries`/`delivery_methods`/`bill_document_types`**:
  catálogos casi estáticos, `woo_code` único → `sap_code`/`sap_sku`.
  **Ya exportados** (2026-08-13, BQI-21) desde la BD MySQL de Integrify-Consola
  con un script puntual, ejecutado una sola vez y eliminado después junto con
  sus credenciales — el proyecto no vuelve a conectarse a esa base. Si en el
  futuro hace falta re-exportar (comunas nuevas, etc.), se rehace el script
  puntualmente en ese momento, no se deja como capacidad permanente.

## Reglas de negocio (heredadas de Integrify-Consola, verificadas en código)

| # | Regla | Detalle |
|---|---|---|
| R1 | `DocDueDate` | = `DocDate`, **no** `commit_date` — si no, el DTE imprime "Crédito" en vez de "Contado" (comentario explícito en `BillingPayload.build`, original) |
| R2 | Troceo de ítems | lotes de 21 líneas por `SAPBilling` (límite de SAP Service Layer) |
| R3 | Espera de folio | polling con `$filter=FolioNumber ne null` — nunca por evento |
| R4 | PDF de Facele/Docele | viene en base64 **doblemente** codificado, decodificar una sola vez |
| R5 | Email cliente | prioriza `contact_email` sobre `customer_email`; BCC = vendedor + destinatarios internos (Strapi en el original → aquí config o tabla propia) |
| R6 | SKU/bodega de línea | se resuelve contra **Stock-Service** (`sync_warehouse`), no contra una tabla propia |
| R7 | Cliente genérico boleta | `CardCode='CN55555555-5'` cuando no hay RUT válido — replicar la misma convención |

## Invariantes de seguridad (con test dedicado — ver BACKLOG.md)

- **I1 — Nunca se marca `COMPLETED` sin confirmación explícita.** `create_sap_invoice` solo si SAP devolvió `DocEntry`; `send_email` solo si Brevo devolvió `messageId`.
- **I2 — Un pedido fallido no bloquea a los demás.** Cada tarea Celery opera sobre un solo `woo_order`/`sap_invoice`/`email`; una excepción no controlada en una tarea no detiene el resto de la cola.
- **I3 — Circuit breaker de volumen.** Si un ciclo de `poll_woo_orders` trae más de `MAX_ORDERS_PER_CYCLE` (configurable, default 50) pedidos nuevos de una vez, se alerta y se procesa igual pero marcado — señal de posible bug de polling/deduplicación, no de operación normal.
- **I4 — Idempotencia por chunk.** `unique(woo_order_id, chunk_index)` en `sap_billings` — reprocesar un pedido no duplica documentos en SAP.
- **I5 — Token-SAP-BQ caído no produce login directo a SAP.** Igual a la I10 de Stock-Service — se porta el mismo test estático (`test_ningun_archivo_hace_login_directo_a_sap`).
- **I6 — Reintentos acotados y visibles.** Ningún job reintenta más allá de su máximo; al agotarse, siempre se inserta una fila en `failures` (nunca falla en silencio).
- **I7 — El PDF nunca se adjunta si Facele no confirmó éxito.** `estado == 1` explícito antes de decodificar y guardar.
- **I8 — Un email nunca se reenvía a quien no corresponde.** Direcciones invalidadas por `is_valid_email` antes de encolar `send_email` → `SKIPPED`, no reintenta indefinidamente.

## Integración con Token-SAP-BQ

Se porta **tal cual** `Stock-Service/app/services/sap/{session,client}.py`
(ya resuelve exactamente esto: sesión cacheada en Redis con TTL derivado de
`expires_at`, invalidación + reintento único ante 401, `TokenSAPBQUnavailableError`
si Token-SAP-BQ no responde — nunca login directo). Único cambio: nuevo
`service_name` propio (`bq-integraciones`, no reutilizar el de Stock-Service
ni el de `gestor-bq`, mismo criterio de auditoría/revocación por separado que
ya aplicaron en ambos proyectos hermanos).

**Acción operativa fuera de código**: agregar
`"bq-integraciones": "<password>"` a `AUTHORIZED_SERVICES` en el `.env` de
Token-SAP-BQ y redeployar ese servicio.

## Integración con Stock-Service (catálogo de productos)

`app/services/stockservice/client.py`, nuevo, delgado:
`get_product(sku) -> {sku, sync_warehouse, price_net, stock_by_warehouse}`
sobre `GET {STOCK_SERVICE_URL}/api/v1/stock/products/{sku}` con header
`X-API-Key`. Caché en Redis (`stockservice:product:{sku}`, TTL 15 min — mismo
orden que el ciclo de sync de Stock-Service, así nunca se sirve un dato más
viejo que su propia fuente) para no golpear la API por cada línea de cada
pedido. Si Stock-Service no responde: `TransientError`, la tarea
`prepare_billing` reintenta — nunca se inventa un SKU/bodega por defecto salvo
que Stock-Service explícitamente no tenga el SKU (ahí sí, mismo fallback
`01` que usa el propio Stock-Service, R5 de su BACKLOG).

## Integración con WooCommerce

Se porta el patrón de `Stock-Service/app/services/woocommerce/client.py`
(Basic Auth, retry con backoff en `urllib3`, hook de `api_logs`), adaptado a
`/orders` en vez de `/products` — mismo criterio de paginación robusto
(`orderby=id&order=asc` + header `X-WP-TotalPages`) en vez del
`len(page) < 100` del original, que puede perder pedidos si una página exacta
de 100 coincide con el corte. Consumer key/secret **propias** de este
proyecto (no reutilizar las de gestorBQ/Integrify-Consola), mismo criterio de
auditoría independiente.

## Despliegue

**Decisión final (no Railway)**: todo el stack corre on-prem, en el mismo
servidor de gestorBQ (`152.230.53.151` / LAN `192.168.0.165`) — no en
Railway. Motivo: Postgres de ese servidor no está expuesto a Internet (ver
`ufw`/`pg_hba.conf`), y replicar ahí el patrón de Stock-Service (Postgres
*managed* en Railway/DigitalOcean) habría requerido abrir esa base a
Internet sin necesidad. `docker-compose.yml` define **solo 3 servicios**
(`api`, `worker`, `beat`) — Postgres (nativo) y Redis (contenedor suelto,
`bq-redis`) ya viven en ese servidor, fuera del compose de la app, igual
que los consume gestorBQ.

| Servicio | Comando | Notas |
|---|---|---|
| `api` | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | expone `8020:8000`, único con healthcheck |
| `worker` | `celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2` | `CELERY_WORKER=1` (NullPool, ver `core/database.py`) |
| `beat` | `celery -A app.tasks.celery_app beat --loglevel=info` | una sola instancia, siempre |

Conexión a Postgres/Redis vía `env_file: .env` + `extra_hosts:
["host.docker.internal:host-gateway"]` — pero el valor de `DATABASE_URL`/
`REDIS_URL` en `.env` depende de DÓNDE corre el compose: IP LAN
(`192.168.0.165`) si se prueba desde una máquina Windows (Docker Desktop no
resuelve `host.docker.internal` hacia otra máquina de la LAN); `host.docker.internal`
si corre en el propio servidor Linux (ahí sí resuelve al mismo host). Dos
`.env` distintos según el contexto, no uno solo.

## Variables de entorno

```
TOKEN_SAP_BQ_URL= / TOKEN_SAP_BQ_SERVICE_NAME=bq-integraciones / TOKEN_SAP_BQ_PASSWORD=
SAP_URL=

STOCK_SERVICE_URL=https://stock-sap-bq-production.up.railway.app
STOCK_SERVICE_API_KEY=

WOO_URL= / WOO_KEY= / WOO_SECRET=

FACELE_URL= / FACELE_USER= / FACELE_PASSWORD= / FACELE_TAXID=

BREVO_API_KEY= / BREVO_TEMPLATE_CUSTOMER_INVOICE= / BREVO_SENDER_NAME= / BREVO_SENDER_EMAIL=
ALERT_EMAILS=

REDIS_URL= / DATABASE_URL=
WOO_POLL_INTERVAL_MINUTES=5 / SAP_INVOICE_POLL_INTERVAL_MINUTES=10
MAX_ORDERS_PER_CYCLE=50

# LEGACY_MYSQL_* ya no aplica — se usó una sola vez para BQI-21 (export de
# catálogos de referencia) y se eliminó del proyecto después de usarla.
```

## Orden de implementación sugerido

1. E0 (bootstrap) + E1 (portar sesión/cliente SAP) — sin esto nada más puede
   probarse contra datos reales.
2. E2 (clientes SAP) — dependencia dura de E3 (facturar exige cliente resuelto).
3. E3 (intake Woo + facturación) — primer tramo end-to-end hasta `doc_entry`.
4. E4 (folio + Facele/Docele).
5. E5 (Brevo).
6. E6 (API/observabilidad) en paralelo desde E1.
7. E7 (tests) — no al final: cada módulo de 1-5 se entrega con su
   `test_<módulo>.py`, igual que hizo Stock-Service (ningún módulo de negocio
   quedó sin test acompañante en ese repo).

Ver `BACKLOG.md` en esta misma carpeta para el detalle de épicas, tickets,
puntos y criterios de aceptación, y para el registro de avance que se irá
completando a medida que se implemente.

## Verificación

- `Stock-Service` real: probar `app/services/stockservice/client.py` contra
  `https://stock-sap-bq-production.up.railway.app/api/v1/stock/products/{sku}`
  con un SKU real antes de integrarlo a `prepare_billing`.
- Token-SAP-BQ real: una vez dado de alta `bq-integraciones` en
  `AUTHORIZED_SERVICES`, `POST /session` debe devolver una sesión válida.
- Extremo a extremo local: `docker compose up`, insertar un `woo_order` de
  prueba (o apuntar `WOO_URL` a un pedido real en ambiente TEST de SAP —
  `CLTSTBIOQUIMICA`), seguir el estado por `GET /status` hasta ver el `email`
  en `COMPLETED`, confirmar la llegada a un destinatario de prueba (nunca un
  cliente real hasta validar todo el camino).
- Forzar cada tipo de fallo (RUT inválido, Token-SAP-BQ apagado, Brevo con
  API key inválida, Stock-Service caído) y confirmar que aparece en
  `failures` sin tumbar el resto de la cola.
- `pytest` completo en verde + `ruff check` limpio antes de cada épica dada
  por cerrada en `BACKLOG.md`.
