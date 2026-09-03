# BQ-Integraciones — Backlog de implementación

Servicio independiente que reemplaza el tramo **Woo → SAP → folio → Facele/Docele → Brevo** del
pipeline de pedidos de bioquimica.cl.

- **Referencia de lógica de negocio:** `Integrify-Consola` (Django, hoy en producción — es lo que
  la web realmente está recibiendo). Ruta: `C:\Users\920562\Desktop\Integrify-consola`.
- **Referencia de arquitectura y código a portar:** `Stock-Service` (FastAPI + SQLModel + Celery +
  Beat + Redis + Alembic + pytest), proyecto hermano ya en producción en Railway. Ruta:
  `C:\Users\920562\Documents\proyectos\Stock-Service`.
- **Sesión SAP:** `Token-SAP-BQ` (servicio centralizado de sesión SAP, Railway). Ruta:
  `C:\Users\920562\Documents\proyectos\Token-SAP-BQ`.
- **Catálogo de productos:** `Stock-Service` en producción — `GET /api/v1/stock/products/{sku}` y
  `GET /api/v1/stock/catalog` (`stock-sap-bq-production.up.railway.app`). No se sincroniza un
  catálogo propio.
- **Estado:** v1 del backlog. Listo para empezar por E0.

Ver `plan.md` en esta misma carpeta para el detalle narrativo de arquitectura, contexto y
decisiones. Este documento es el de **seguimiento de avance** — se actualiza a medida que se
implementa cada ticket.

---

## 1. Objetivo

Replicar, en un proyecto nuevo y liviano, el tramo de Integrify-Consola que factura pedidos web en
SAP, espera el folio del DTE, obtiene el PDF de Facele/Docele y lo envía por Brevo — con manejo de
errores explícito por ítem (un pedido que falla nunca detiene a los demás) y backpressure (la
profundidad de la cola amortigua, no se dispara más carga de la que SAP/Facele/Brevo toleran).

## 2. Alcance

**v1:**
- Resolución/creación de Business Partner en SAP (RUT, comuna, direcciones) — dependencia dura de
  facturar, replicada completa (no asumida).
- Ingesta de pedidos WooCommerce por polling, transformación a facturación SAP (troceo de 21 ítems +
  envío), resolución de SKU/bodega **vía Stock-Service** (no catálogo propio).
- Creación de la facturación en SAP Service Layer.
- Espera del folio por polling (`FolioNumber ne null`).
- Obtención del PDF desde Facele/Docele (SOAP), decodificación doble base64.
- Envío del PDF por Brevo, con alertas internas ante fallo definitivo.
- API de consulta/estado, reintentos acotados y visibles, tests con cobertura de invariantes.

**Fuera de v1:** sincronización propia de catálogo de productos (vive en Stock-Service) ·
sincronización de stock/precio hacia WooCommerce (no es este pipeline) · UI de administración
(alcanza con `/status`, `/failures`, admin de BD directo) · cualquier lógica de despacho/courier
(vive en `gestorBQ`, dominio distinto).

## 3. Arquitectura

| Componente | Tecnología | Nota |
|---|---|---|
| API | FastAPI + Uvicorn | disparo manual y consulta |
| ORM | SQLModel (SQLAlchemy 2 async) + asyncpg | |
| DB | PostgreSQL | nativo en servidor on-prem (compartido con gestorBQ, base propia) |
| Migraciones | Alembic | |
| Cola / scheduler | Celery worker + Beat | igual patrón que Stock-Service |
| Broker / lock / caché | Redis | lock distribuido, caché de sesión SAP y de catálogo Stock-Service |
| **Sesión SAP** | **Token-SAP-BQ** | `POST /session` / `POST /session/invalidate`; sin login propio |
| **Catálogo de productos** | **Stock-Service** | `GET /stock/products/{sku}`; sin sync propio |
| Cliente Woo | `requests` + Basic Auth, paginación `X-WP-TotalPages` | |
| Facele/Docele | SOAP (`xmltodict`) | `DoceleOL_Auth/DocumentosEmitidosService` |
| Brevo | REST + API key | adjunto PDF |
| Observabilidad | Sentry + `api_logs` + alertas por correo | |
| Despliegue | docker-compose en servidor on-prem (api/worker/beat; Postgres/Redis ya viven ahí, fuera del compose) | |

```
BQ-Integraciones/
├── app/
│   ├── api/routes/{orders,billing,invoices,failures}.py
│   ├── core/{config,database,logging,sentry,api_log}.py
│   ├── models/{woo_order,sap_customer,sap_billing,sap_invoice,email,failure,sync_run,reference_data}.py
│   ├── pipelines/{customers,billing,invoices,documents,notifications}.py
│   ├── services/sap/{session,client}.py
│   ├── services/woocommerce/client.py
│   ├── services/facele/client.py
│   ├── services/brevo/client.py
│   ├── services/stockservice/client.py
│   └── tasks/{celery_app,scheduled,locks,heartbeat}.py
├── alembic/ · tests/ · docs/RUNBOOK.md · backlog/{plan,BACKLOG}.md
└── docker-compose.yml · Dockerfile · .dockerignore · pyproject.toml
```

## 4. Flujo del pipeline (7 fases)

```
FASE 1  resolve_customer   Woo → SAP        RUT válido, comuna mapeada, BP creado/actualizado
FASE 2  prepare_billing     DB → DB          trocea ítems (21) + envío, resuelve SKU/bodega vía Stock-Service
FASE 3  create_sap_invoice  DB → SAP         POST /Invoices — crea el documento (aún sin folio)
FASE 4  poll_sap_invoices   SAP → DB         filtro FolioNumber ne null — la espera del folio ES este polling
FASE 5  fetch_pdf           Docele → DB      SOAP, decodifica base64 doble
FASE 6  prepare_email       DB → DB          valida destinatarios, construye payload
FASE 7  send_email          DB → Brevo       adjunta PDF, envía
```

Cada fase = una tarea Celery + un `SyncRun`. La fase 4 corre en su propio ciclo de Beat, desacoplada
de las fases 1-3 (igual que en el original, dos cron separados).

## 5. Modelo de datos

Estado común a toda tabla de trabajo: `status` (`PENDING|IN_PROGRESS|COMPLETED|FAILED|SKIPPED|EXHAUSTED`),
`status_message`, `attempts`, `last_attempt_at`, `created_at`/`updated_at`.

- **`woo_orders`** — `code` (único) · `reference` · `paid_at` · `total` · `shipping` ·
  `delivery_method_code` · `bill_doc_type_code` · `customer_tax_id` · `billing_address`/`shipping_address` (JSON) ·
  `items` (JSON, snapshot inmutable)
- **`sap_customers`** — `tax_id` (único) · `code` · `contact_code`/`contact_name` ·
  `bill_code`/`bill_row` · `ship_code`/`ship_row` · `business_activity` · `exists` (bool)
- **`sap_billings`** — `woo_order_id` FK · `chunk_index` (unique con woo_order_id) · `doc_type_code` ·
  `total` · `doc_date` · `items` (JSON resuelto) · `doc_entry` · `doc_num`
- **`sap_invoices`** — `sap_billing_id` FK · `doc_entry` (único) · `folio` · `folio_prefix` ·
  `customer_email`/`contact_email`/`seller_email` · `pdf_base64`
- **`emails`** — `sap_invoice_id`/`woo_order_id` FK · `event_type` (`CUSTOMER_INVOICE|INTERNAL_ALERT`) ·
  `to`/`bcc` (JSON) · `brevo_message_id`
- **`failures`** — `entity_type` · `entity_id` · `stage` · `error_message` · `attempts` ·
  `occurred_at` · `notified`
- **`sync_runs`** — `pipeline` · `triggered_by` · `status` · tiempos · contadores
- **`municipalities`/`industries`/`delivery_methods`/`bill_document_types`** — catálogos estáticos,
  `woo_code` único → `sap_code`/`sap_sku`, poblados por `scripts/seed_from_legacy.py`

## 6. Reglas de negocio

| # | Regla | Detalle |
|---|---|---|
| R1 | `DocDueDate` | = `DocDate`, no `commit_date` — si no, el DTE imprime "Crédito" en vez de "Contado" |
| R2 | Troceo de ítems | lotes de 21 líneas por `SAPBilling` (límite SAP Service Layer) |
| R3 | Espera de folio | polling `$filter=FolioNumber ne null`, nunca por evento |
| R4 | PDF Facele/Docele | base64 **doblemente** codificado, decodificar una sola vez |
| R5 | Email cliente | `contact_email` > `customer_email`; BCC = vendedor + internos |
| R6 | SKU/bodega de línea | resuelto contra Stock-Service (`sync_warehouse`), no tabla propia |
| R7 | Cliente genérico boleta | `CardCode='CN55555555-5'` si no hay RUT válido — **no implementada a propósito** (2026-08-14): en Integrify no es una bifurcación real, es solo el `CardCode` resultante si Woo manda ese RUT puntual; verificado en Postgres real que 0/41 pedidos (Factura y Boleta) tienen `customer_tax_id` nulo. Se deja el `PermanentError` actual como red de seguridad hasta que aparezca un caso real en `/failures` |
| R8 | SKU de envío por courier | La línea de despacho en `DocumentLines` (R2) debe usar el `ItemCode` real del courier/subvariante (`delivery_method_code`/`courier_code` de BioCommerce), no un SKU genérico — **hoy no implementado**: cae siempre a `SG000096` porque `delivery_methods` no tiene cargados los couriers del sitio nuevo (hallazgo 2026-09-02, ver registro E3) |

## 7. Invariantes de seguridad

Cada una debe tener al menos un test dedicado (`test_invariants_coverage.py`, ver E7/BQI-71).

- **I1** — Nunca se marca `COMPLETED` sin confirmación explícita (DocEntry de SAP, messageId de Brevo).
- **I2** — Un pedido fallido no bloquea a los demás (aislamiento por tarea Celery).
- **I3** — Circuit breaker de volumen: más de `MAX_ORDERS_PER_CYCLE` pedidos nuevos en un ciclo → alerta.
- **I4** — Idempotencia por chunk: `unique(woo_order_id, chunk_index)` en `sap_billings`.
- **I5** — Token-SAP-BQ caído nunca produce login directo a SAP (igual a I10 de Stock-Service).
- **I6** — Reintentos acotados y visibles: al agotarse, siempre una fila en `failures`.
- **I7** — El PDF nunca se adjunta si Facele no confirmó `estado==1`.
- **I8** — Un email nunca se reenvía a una dirección inválida (se marca `SKIPPED`, no reintenta indefinidamente).

## 8. Decisiones tomadas

**D1 — Stack.** FastAPI + SQLModel + Celery + Beat + Redis + Alembic, calcado de `Stock-Service`
(no Django, no RQ). Motivo: proyecto hermano ya prueba este stack en producción contra los mismos
sistemas externos.

**D2 — Catálogo de productos.** Se consume `Stock-Service` (`GET /stock/products/{sku}`) con caché
Redis de 15 min. No se construye ni mantiene un espejo propio de productos.

**D3 — Cliente SAP.** Se replica completo el módulo de clientes de Integrify-Consola (creación de
Business Partner, no solo lookup) — un pedido de un cliente nuevo no debe quedar bloqueado
esperando intervención manual.

**D4 — Intake.** Polling programado a WooCommerce, no webhook — no se necesita reacción instantánea
y evita exponer un endpoint público adicional.

**D5 — Orquestación.** Celery+Redis acepta explícitamente el trade-off de aislar errores por ítem y
dar backpressure vía profundidad de cola, sobre reintentos manuales `for/try/except/continue` del
original.

**D6 — gestorBQ descartado como dependencia.** Es un portal de logística/despachos sin relación de
dominio con este pipeline (confirmado con el usuario) — solo aportó el estilo de los clientes de
ejemplo (`integraciones/sap_client.py`/`woo_client.py`), que quedan como referencia histórica, no
como base de código a extender.

## 9. Épicas y tickets

Puntos relativos (1 ≈ media jornada). Prefijo de ticket: **BQI-**.

### E0 — Bootstrap (12 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-01 | Esqueleto `pyproject.toml`, ruff, pytest, `.env.example` | 2 | `uv sync` instala; `ruff check` limpio |
| BQI-02 | `core/config.py` (Pydantic Settings): SAP, Token-SAP-BQ, Stock-Service, Woo, Facele, Brevo, DB, Redis, umbrales | 2 | Arranque falla con mensaje claro si falta una variable obligatoria |
| BQI-03 | `core/database.py` async + `NullPool` en worker | 1 | Dos tareas Celery seguidas sin "Event loop is closed" |
| BQI-04 | Alembic + migración inicial (todas las tablas de §5) | 2 | `alembic upgrade head` crea el esquema desde cero |
| BQI-05 | `celery_app.py` + Beat + `locks.py` + `heartbeat.py` | 3 | Dos disparos simultáneos del mismo poll → el segundo responde `skipped: lock` |
| BQI-06 | Dockerfile + docker-compose (api, worker, beat — Postgres/Redis viven fuera, en el servidor) | 2 | `docker compose up` levanta el stack y `/health` responde 200 |

### E1 — Sesión y cliente SAP (8 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-10 | Portar `services/sap/session.py` de Stock-Service, `service_name=bq-integraciones` | 2 | Sesión compartida vía Token-SAP-BQ; cero llamadas a `/Login` |
| BQI-11 | Portar `services/sap/client.py`, **extender con soporte POST/PATCH** (Stock-Service solo tiene GET) | 2 | `$`-safe URL, retry, timeouts, `api_logs`; POST/PATCH funcionando contra SAP TEST |
| BQI-12 | Alta de `bq-integraciones` en `AUTHORIZED_SERVICES` de Token-SAP-BQ + verificación real | 1 | `POST /session` devuelve sesión válida |
| BQI-13 | `get_all_pages` reusado tal cual | 1 | Catálogo/listado paginado completo sin duplicados |
| BQI-14 | Tests de sesión/cliente (portar `test_sap_session.py`) | 2 | I5 cubierta, incluyendo el chequeo estático anti-login-directo |

### E2 — Clientes SAP: resolución y creación de Business Partner (15 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-20 | Modelo `SAPCustomer` + tablas `municipalities`/`industries` | 2 | Migración aplica sin errores |
| BQI-21 | `scripts/seed_from_legacy.py` — export desde MySQL de Integrify-Consola (municipios, industrias, delivery_methods, bill_document_types) | 2 | Conteo de filas coincide con el original |
| BQI-22 | Validación de RUT chileno (`rut_chile`) | 1 | RUT inválido → `FAILED` inmediato, sin reintento |
| BQI-23 | `find_by_rut` contra `BusinessPartners` de SAP | 2 | Cliente existente se encuentra por RUT |
| BQI-24 | Sanitización de textos a límites de campo SAP (nombre, giro, email, teléfono, dirección) | 3 | Puerto de `app/customers/models/sap_customer.py::clean()` |
| BQI-25 | `create_or_update` (POST si no existe, PATCH si existe, código `CN{tax_id}` por defecto) | 3 | Cliente nuevo queda creado en SAP TEST con contacto y direcciones |
| BQI-26 | Pipeline `resolve_customer()` completo + tests | 2 | Cliente inexistente en SAP → se crea; cliente existente → se actualiza |

### E3 — Intake WooCommerce y facturación (18 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-30 | `WooCommerceClient` de órdenes, paginación `X-WP-TotalPages` | 2 | 500+ pedidos recorridos sin duplicados ni saltos |
| BQI-31 | `poll_woo_orders()` + dedup por `code` + circuit breaker I3 | 3 | Reejecutar sin pedidos nuevos → 0 encolados; >`MAX_ORDERS_PER_CYCLE` → alerta |
| BQI-32 | `services/stockservice/client.py::get_product(sku)` + caché Redis | 2 | Segunda consulta al mismo SKU no golpea la API |
| BQI-33 | `prepare_billing()`: troceo de 21 ítems + ítem de envío + resolución SKU/bodega | 4 | Pedido de 45 ítems → 3 `SAPBilling` (21/21/3) |
| BQI-34 | Validación de totales (`WooOrder.total == Σ SAPBilling.total`) | 2 | Discrepancia → `FAILED` con mensaje explícito |
| BQI-35 | `create_sap_invoice()`: `BillingPayload` (R1) + `POST /Invoices` | 3 | Documento creado en SAP TEST con `DocDueDate == DocDate` |
| BQI-36 | Tests de billing con fixtures Woo/SAP | 2 | Cobertura de R1, R2, I4 |
| BQI-37 | Idempotencia externa en `create_sap_invoice`: buscar factura existente en SAP antes de crear (`U_WedDocNum`+`DocTotal`+`U_IX_Ind`) | 2 | Reintentar sobre un chunk cuyo `POST /Invoices` ya fue aceptado por SAP (pero no se llegó a commitear localmente) adopta el `DocEntry` existente en vez de duplicar la factura |

### E4 — Folio y Facele/Docele (12 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-40 | `poll_sap_invoices()`: filtro `FolioNumber ne null`, dedup por `doc_entry` | 3 | Factura sin folio no aparece; con folio, aparece una sola vez |
| BQI-41 | `services/facele/client.py`: SOAP `DoceleOL_Auth/DocumentosEmitidosService` | 3 | Respuesta XML parseada a modelo Pydantic |
| BQI-42 | Decodificación doble base64 (R4) + validación `estado==1` (I7) | 2 | PDF corrupto o `estado==0` → `FAILED`, nunca se guarda |
| BQI-43 | `fetch_pdf()` pipeline completo + reintentos (máx. 5, backoff largo) | 2 | Folio recién emitido (aún no en Docele) → reintenta, no falla definitivo de inmediato |
| BQI-44 | Tests con fixtures Facele | 2 | Cobertura de R4, I7 |

### E5 — Notificaciones Brevo (10 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-50 | `services/brevo/client.py`: `POST smtp/email` con adjunto | 2 | Correo de prueba recibido con PDF adjunto |
| BQI-51 | `prepare_email()`: destinatarios (R5), validación de direcciones (I8) | 3 | Email inválido → `SKIPPED`, no bloquea el pedido |
| BQI-52 | `send_email()` + marca `sent` con `messageId` (I1) | 2 | Fallo de Brevo → `FAILED`, reintenta hasta máx. 3 |
| BQI-53 | `notify_failure()`: alerta interna con lock anti-spam 1h | 2 | 10 fallos del mismo tipo en 1h → un solo correo |
| BQI-54 | Tests con fixtures Brevo | 1 | Cobertura de R5, I1, I8 |

### E6 — API, scheduler y observabilidad (10 pts)

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-60 | `GET /health`, `GET /status` (conteos por tabla/estado) | 2 | `/status` refleja el estado real de la BD |
| BQI-61 | `GET /failures`, `POST /retry/{tabla}/{id}` | 2 | Reintentar un fallo desde la API vuelve a encolar la tarea |
| BQI-62 | Beat schedule definitivo + heartbeat Healthchecks.io | 2 | Ping registrado en cada ciclo exitoso |
| BQI-63 | Sentry + `api_logs` | 2 | Excepción no controlada aparece en Sentry con contexto |
| BQI-64 | Middleware API Key (portar de Stock-Service) | 1 | Petición sin `X-API-Key` → 401 salvo `/health` |
| BQI-65 | `docs/RUNBOOK.md` | 1 | Documento con qué mirar ante una alerta |

### E7 — Pruebas (8 pts, transversal)

No se implementa al final — cada ticket de E1-E6 se entrega junto con su test. Esta épica cubre lo
transversal:

| ID | Ticket | Pts | Criterio de aceptación |
|---|---|---|---|
| BQI-70 | `conftest.py` + `fixtures/` (payloads SAP, Woo, Facele, Brevo) | 2 | Tests corren sin red ni Docker (SQLite en memoria vía `aiosqlite`) |
| BQI-71 | `test_invariants_coverage.py` (I1-I8) | 2 | Falla si alguna invariante pierde su test asociado |
| BQI-72 | Integración del ciclo completo contra mocks (E2E) | 3 | Pedido simulado de punta a punta → `email.status == COMPLETED` |
| BQI-73 | CI (GitHub Actions): pytest + ruff en cada push | 1 | PR bloqueado si algo falla |

**Total: ~95 pts** (incluye BQI-37, agregado 2026-08-14). Orden: E0 → E1 → E2 → E3 → E4 → E5 → E6, con E7 transversal a todas.

---

## 10. Registro de avance

Se actualiza a medida que se cierra cada ticket: `[ ]` pendiente, `[~]` en curso, `[x]` hecho, con
fecha y nota breve si algo se desvió del plan.

### E0 — Bootstrap
- [x] BQI-01 — Esqueleto del repo (2026-08-13)
- [x] BQI-02 — `core/config.py` (2026-08-13)
- [x] BQI-03 — `core/database.py` (2026-08-13) — verificado con conexión real a Postgres
- [x] BQI-04 — Alembic + migración inicial (2026-08-13)
- [x] BQI-05 — Celery + Beat + locks + heartbeat (2026-08-13) — verificado con disparo manual + Docker
- [x] BQI-06 — Dockerfile + docker-compose (2026-08-13) — build real + `docker compose up` verificados

### E1 — Sesión y cliente SAP
- [x] BQI-10 — Portar `sap/session.py` (2026-08-13) — probado contra Token-SAP-BQ real
- [x] BQI-11 — Portar `sap/client.py` + soporte POST/PATCH (2026-08-13) — probado contra SAP real
- [ ] BQI-12 — Alta en Token-SAP-BQ — **diferido a antes de producción**, hoy usa credenciales de test de gestorBQ a propósito (ver `memory/project_credenciales_test_vs_prod.md`)
- [x] BQI-13 — `get_all_pages` (2026-08-13) — probado contra SAP real (21.157 registros paginados)
- [x] BQI-14 — Tests de sesión/cliente (2026-08-13) — 10/10 tests, portados de Stock-Service

### E2 — Clientes SAP
- [x] BQI-20 — Modelo `SAPCustomer` + catálogos (2026-08-13) — incluye `DeliveryMethod`/`BillDocumentType`, no solo municipios/industrias como decía el ticket original
- [x] BQI-21 — export puntual desde Integrify-Consola (2026-08-13) — 97 municipios, 9 industrias, 3 métodos de envío, 9 tipos de documento cargados en Postgres; script y credenciales eliminados después (ver nota)
- [x] BQI-22 — Validación RUT (2026-08-13) — `app/utils/rut.py::es_rut_valido()`, envuelve `rut_chile` para que nunca lance excepción; 4/4 tests
- [x] BQI-23 — `find_by_rut` (2026-08-13) — `app/services/sap/customers.py`, probado contra SAP real con RUT `70990700-K`: **hallazgo** — hay 2 BP duplicados en SAP para ese RUT (`CN70990700-K`/`CN70.990.700-k`), decidir criterio de desambiguación en BQI-25/26; 4/4 tests
- [x] BQI-24 — Sanitización de textos (2026-08-13) — `app/utils/sap_text.py::sanitizar_texto_sap()` (puerto de `utils/sap.py` original) + `app/utils/rut.py::normalizar_rut()`; confirmado contra el hallazgo de BQI-23 (sin puntos es el formato que SAP encuentra); 9/9 tests
- [x] BQI-25 — `create_or_update` (2026-08-13) — `CustomerPayload`/`create_or_update` en `app/services/sap/customers.py`, 5/5 tests con mock + probado contra SAP real (RUT propio de Felipe, autorizado, `CN21269680-3`): PATCH llegó, SAP devolvió error de negocio real (`-2035 ContactEmployees.Name` duplicado) — confirma que el mecanismo funciona; el error confirma que hace falta la lógica de "código/dirección disponible" de BQI-26 antes de actualizar un cliente existente
- [x] BQI-26 — Pipeline `resolve_customer()` (2026-08-13) — `app/pipelines/customers.py`, 4/4 tests con mock + verificado de punta a punta contra SAP y Postgres reales (RUT propio de Felipe): status COMPLETED, reutilizó contacto/direcciones existentes (65553/FISCAL/DESPACHO) sin colisión, sanitización confirmada en producción (SAP). **Épica E2 completa.**

**Nota BQI-21 (2026-08-13):** Felipe frenó la ejecución al ver que el script se conectaba a la MySQL
de producción de Integrify-Consola (DigitalOcean) para preguntar por qué — comportamiento ya
documentado en `plan.md` §"Migración de datos maestros", pero sin confirmación explícita antes de
ejecutar la conexión real. Confirmado el alcance (export puntual, una sola vez, sin uso recurrente),
se corrió: `defaultdb` estaba vacía, la base real es `app` (municipality/sap_municipality/
woo_municipality/industry/delivery_method/bill_document_type). Extracción exitosa, datos verificados
en Postgres. Por pedido explícito de Felipe, se eliminó después TODA referencia a esa conexión:
`scripts/seed_from_legacy.py`, `LEGACY_MYSQL_*` de `.env`/`.env.example`/`core/config.py`, y la
dependencia `pymysql` del `pyproject.toml`. El proyecto no vuelve a tocar esa base.

**Corrección BQI-21 (2026-08-13, encontrada al construir el glue de E6/BQI-26):** el export original
de comunas estaba **incompleto** — filtraba `sap_municipality.enabled=1`, que en la MySQL de
Integrify NO significa "mapeo válido" (250 de 349 filas tienen `enabled=0`, incluyendo comunas reales
en uso, ej. Angol/`CL_107`). De 32 comunas distintas usadas en los 42 pedidos reales ya guardados,
**0/32** matcheaban nuestra tabla de 97 filas. Se reconectó una vez más (mismo procedimiento: script
aislado en `scratchpad`, fuera del proyecto, credenciales borradas al terminar) para extraer el join
completo sin ese filtro (`municipality ⨝ sap_municipality` por `sap_municipality_id=code`, sin
condición sobre `enabled`) — **346 filas** (el total real de comunas de Chile), de las cuales las 97
anteriores coincidían exactamente (no eran incorrectas, solo incompletas). `industries`/
`delivery_methods`/`bill_document_types` se revisaron por el mismo patrón y NO tienen el problema
(tablas planas, sin split de 3 tablas, todas `enabled=1`). Tabla `municipalities` en Postgres ahora
tiene 346 filas correctas.

### E3 — Intake Woo y facturación
- [x] BQI-30 — `WooCommerceClient` de órdenes (2026-08-13) — `app/services/woocommerce/{client,orders}.py`, probado contra bioquimica.cl real (3003 pedidos `processing`, paginación `X-WP-TotalPages` confirmada). Diseño con miras a que la web nueva cambie el checkout: la transformación de pedido crudo → `woo_orders` (BQI-31) va función-por-campo, no un mapeo único, para que un cambio de payload sea un ajuste acotado (ver `memory/project_woo_payload_cambiante.md`)
- [x] BQI-31 — `poll_woo_orders()` + circuit breaker (2026-08-13) — `app/models/woo_order.py` + `app/pipelines/woo_orders.py`, 10/10 tests + verificado contra bioquimica.cl y Postgres reales (41 pedidos guardados, dedup confirmado en 2da corrida). Bug real encontrado por los tests antes de producción: `paid_at` llegaba como string, la columna es datetime — corregido con `datetime.fromisoformat`. También corregido bug de mixin: `SyncStatusMixin` compartía una instancia de `Column` entre modelos (`sa_column`→`sa_type`), rompía al agregar el segundo modelo
- [x] BQI-32 — Cliente Stock-Service (2026-08-13) — `app/services/stockservice/client.py::obtener_producto()`, 3/3 tests + verificado contra Stock-Service y Redis reales con SKU real (`ML000275`, `woo_id=7685` coincide con `product_id` del pedido real — confirma criterio de matching para BQI-33), TTL de caché confirmado (~900s)
- [x] BQI-33 — `prepare_billing()` (2026-08-13) — `app/models/sap_billing.py` + `app/pipelines/billing.py`, 11/11 tests + verificado contra 2 pedidos reales (flat_rate con envío: totales calzan exacto incl. IVA de envío `ROUND_HALF_UP`; free_shipping sin ítem de envío) e idempotencia confirmada en Postgres real (1 fila tras 2 corridas)
- [x] BQI-34 — Validación de totales (2026-08-13) — dentro de `prepare_billing`; corregido para que además marque `WooOrder.status=FAILED` con mensaje (no solo lanzar excepción) — criterio real del ticket, 2 tests nuevos
- [x] BQI-35 — `create_sap_invoice()` (2026-08-13) — `app/services/sap/billing.py` (`BillingPayload`/`BillingItemPayload`) + orquestador en `app/pipelines/billing.py`, 4/4 tests + verificado contra SAP real: factura creada (`DocEntry=103959`, `DocNum=7412`), R1 confirmado en SAP (`DocDate == DocDueDate == "2026-08-13"`). Hallazgo operativo: SAP rechaza CUALQUIER documento si falta la tasa de cambio USD del día — dependencia diaria a tener en cuenta para producción, no es un bug nuestro
- [x] BQI-36 — Tests de billing (2026-08-13) — cubierto a lo largo de BQI-33/34/35 (24 tests entre `test_billing.py`/`test_create_sap_invoice.py`), no quedó pendiente aparte.
- [ ] **SKU de envío por courier real — pendiente de implementar** (2026-09-02, no ticketeado —
  hallazgo real durante prueba end-to-end con pedido de prueba `9234`) — `_buscar_metodo_entrega()` +
  `_item_envio()` (`app/pipelines/billing.py`) ya arman bien la línea de envío en `DocumentLines` con
  el monto neto sin IVA (R2, esto sí está implementado y probado), pero el SKU sale de la tabla
  `delivery_methods`, cargada en BQI-21 con solo 3 filas **legacy del sitio viejo** (Integrify/
  bioquimica.cl) — sin los couriers del sitio **nuevo** (BioCommerce). Ya estaba anotado como
  comentario suelto en `docs/ejemplo-payload-sap-factura.comentado.jsonc:54` ("courier específico,
  aún no mapeado en delivery_methods; hoy siempre sale SG000096 genérico") pero nunca se había vuelto
  ticket. No es alcance de gestorBQ (la exclusión de "despacho/courier" en §2/D6 es sobre *logística*
  de envío, no sobre el SKU de facturación de la línea en SAP, que sí es de este proyecto — distinción
  que el backlog no dejaba clara). `delivery_method_code` (=`courier_code` de BioCommerce, ver
  `_bc_extraer_delivery_method_code()` en `woo_orders.py`) coincide 1:1 con el `ItemCode` SAP
  (confirmado contra SAP real, `campos-payload-sap.md`) — falta cargar las filas.
  **SKUs confirmados por Felipe (2026-09-02)**: `SGmoveup` → MoveUp · `SGchistn` → Chibra Terrestre ·
  `SGchiexp` → Chibra Express. **Starken queda pendiente** (SKU aún no definido). Falta: insertar estas
  3 filas en `delivery_methods` (mapeo identidad `woo_code`=`sap_sku`) + agregar Starken cuando se
  defina + test de regresión que confirme que un `courier_code` real ya no cae al genérico.
- [x] BQI-37 — Idempotencia externa `create_sap_invoice` (2026-08-17) — `buscar_factura_existente()` en `app/services/sap/billing.py` + guard de estado y guard robusto en `app/pipelines/billing.py::create_sap_invoice`, 9/9 tests (`test_create_sap_invoice.py`/`test_sap_billing_service.py` nuevo). **Bug real encontrado al probar contra SAP real**: `U_WedDocNum` está tipado como *string* en SAP pese a representar un número — filtrar sin comillas devuelve `400` ("the given value is not a string"); corregido, confirmado `200` contra SAP real. **Épica E3 completa.**

### E4 — Folio y Facele/Docele
- [x] BQI-40 — `poll_sap_invoices()` (2026-08-13) — `app/models/sap_invoice.py` + `app/pipelines/invoices.py`, 3/3 tests + verificado contra SAP/Postgres reales (factura de prueba sin folio → `nuevas=0`, sin explotar). `seller_email` queda sin llenar a propósito, se resuelve en BQI-51
- [x] BQI-41 — Cliente Facele/Docele (2026-08-13) — `app/services/facele/client.py::obtener_documento()`, verificado contra **producción** real (folio 42742, tipo 33): `estado=1`, PDF recibido. Sin ambiente de test usable (ver `memory/project_facele_test_es_produccion.md`)
- [x] BQI-42 — Decodificación + validación de estado (2026-08-13) — `decodificar_pdf()`, confirmado con el folio real: 1 decode da base64 válido, un 2do decode (solo para verificar, no se hace en el pipeline) da PDF real (`%PDF-1.4`). 7/7 tests
- [x] BQI-43 — Pipeline `fetch_pdf()` (2026-08-13) — `app/pipelines/documents.py`, verificado contra Facele producción + Postgres real (folio 42742, `DocEntry` de prueba `999999002`): `status=COMPLETED`, PDF de 128.796 caracteres persistido
- [x] BQI-44 — Tests con fixtures Facele (2026-08-13) — 5/5 tests (`test_documents.py`), cubren estado exitoso, estado=0, estado=1 sin PDF, PDF malformado, error de red. **Épica E4 completa.**

### E5 — Notificaciones Brevo
- [x] BQI-50 — Cliente Brevo (2026-08-13) — `app/services/brevo/client.py`, `BREVO_URL` a config (no hardcodeado)
- [x] BQI-51 — `prepare_email()` (2026-08-13) — `app/models/email.py` (tabla `emails` nueva, migración aplicada) + `app/pipelines/notifications.py`. R5 confirmado: `contact_email` > `customer_email`; BCC = `BREVO_INVOICE_BCC` + `seller_email`. I8: sin destinatario o dirección inválida → `SKIPPED`
- [x] BQI-52 — `send_email()` (2026-08-13) — I1: solo `COMPLETED` con `messageId` real de Brevo. Agregado freno no planeado originalmente: fuera de `ENVIRONMENT=production`, el destinatario real se redirige a `ALERT_EMAILS` y el asunto lleva `[PRUEBA]` — así ninguna prueba/automatización en dev puede llegarle a un cliente real aunque la fila tenga su email de verdad
- [x] BQI-53 — `notify_failure()` (2026-08-13) — anti-spam por `kind` con Redis `INCR`+`EXPIRE` (ventana 1h), fail-open si Redis no responde. No persiste fila en `emails` (alerta best-effort, sin retry propio)
- [x] BQI-54 — Tests (2026-08-13) — 18/18 tests (`test_notifications.py`), cubre R5, I1, I8 + el freno de entorno. **Épica E5 completa.** 97/97 tests del proyecto
- [x] Guard de idempotencia en `prepare_email`/`send_email` (2026-08-18, no ticketeado — parte 2 del hallazgo de resiliencia post-caída junto con BQI-37) — `if status == "COMPLETED": return` al inicio de ambas funciones en `app/pipelines/notifications.py`, evita reenviar un correo real al cliente ante un reintento. 2 tests nuevos en `test_notifications.py`. 112/112 tests del proyecto

**Cambio de transporte (2026-08-18):** `notify_failure()` dejó de usar Brevo — a pedido explícito de
Felipe, las alertas internas de error van por **SMTP directo** (Gmail/Google Workspace,
`smtp.gmail.com:587`, cuenta `no-reply@bioquimica.cl`), separado del canal de Brevo que sigue
siendo exclusivo para `CUSTOMER_INVOICE`. Nuevo `app/services/smtp/client.py` (smtplib + STARTTLS).
`EMAIL_SMTP_HOST`/`EMAIL_SMTP_PORT`/`EMAIL_SENDER`/`EMAIL_PASSWORD` en `.env`/`core/config.py`.
Tests actualizados (incluye regresión explícita "notify_failure no debe usar brevo_client"). Probado
en vivo: `notify_failure()` real devolvió `True`, correo real enviado a felipe.morales@bioquimica.cl.
154/154 tests, `ruff check .` limpio.

**Verificación real (2026-08-13):** `prepare_email`+`send_email`+`notify_failure` corridos contra Brevo real (factura de prueba `doc_entry=999999002`/folio `42742`, `notify_failure("verificacion_bqi53", ...)`). Hallazgo operativo: Brevo bloqueó el primer intento por IP no autorizada del servidor on-prem (`152.230.53.151`) — no es bug, hay que autorizar la IP en Brevo (Security → Authorised IPs) una sola vez; ya autorizada, el reintento devolvió `messageId` real (`...@smtp-relay.mailin.fr`) y `notify_failure` devolvió `True`. `BREVO_SENDER_NAME`/`EMAIL`/`ALERT_EMAILS`/`BREVO_INVOICE_BCC` reales (external/failure recipients de Strapi de Integrify) quedan diferidos a antes de producción — hoy `.env` solo apunta a felipe.morales@bioquimica.cl (ver `memory/project_brevo_destinatarios.md`).

### E6 — API, scheduler y observabilidad
- [x] Glue `construir_datos_cliente()` (2026-08-13, no ticketeado originalmente) — `app/pipelines/customers.py`. Hueco real encontrado al preparar el wiring de Beat: `resolve_customer()` esperaba un dict que nada armaba desde `WooOrder.billing_address`/`shipping_address`. Mapeo confirmado línea por línea contra `app/customers/models/sap_customer.py::clean()` de Integrify (no un comando separado). 9/9 tests nuevos + verificado contra 5 pedidos reales guardados (sin tocar SAP). **Corrigió en el camino un bug real de datos**: el catálogo `municipalities` (BQI-21) estaba incompleto — filtraba `sap_municipality.enabled=1` en la MySQL de Integrify, que no significa "mapeo válido" (0/32 comunas reales de los 42 pedidos guardados matcheaban). Re-extraído sin ese filtro: 346 comunas (antes 97), ver nota en BQI-21 arriba
- [x] BQI-60 — `/health`, `/status` (2026-08-18) — `app/main.py` (no existía, pese a que Dockerfile/docker-compose ya apuntaban a `app.main:app` desde BQI-06) + `app/api/routes/status.py`. Probado con `uvicorn` local contra Postgres real: ambos responden 200, `/status` refleja conteos reales (42 woo_orders, etc.). 112/112 tests
- [x] BQI-61 — `/failures`, `/retry` (2026-08-18) — modelo `Failure` (migración `79e3d05f4c0a` aplicada contra Postgres real; bugs de la migración autogenerada corregidos: faltaba `import sqlmodel`, `error_message` quedaba `nullable=True` pese a ser obligatorio). `app/api/routes/failures.py`: `GET /failures` + `POST /retry/{tabla}/{entity_id}` como **llamada síncrona directa** a la función de pipeline correspondiente (decisión explícita: las fases 3/5/6/7 no tienen tarea Celery propia todavía — encolar de verdad es trabajo de conectar el orquestador real, fuera de este ticket). `sap_customers` es el caso especial: reconstruye `datos_cliente` buscando el `WooOrder` más reciente con el mismo `tax_id`, ya que `SAPCustomer` no guarda a qué pedido pertenece. **Bug real encontrado por los tests antes de producción**: el `except Exception` de `reintentar()` también atrapaba los `HTTPException` (422) que lanzan `_reintentar_sap_customer`/`_reintentar_sap_billing` cuando no encuentran el `WooOrder` asociado — los silenciaba y devolvía 200 en vez de propagar el error; corregido con `except HTTPException: raise` antes del catch genérico. Probado en vivo contra Postgres real (`GET /failures` vacío, 404 tabla inválida, 404 id inexistente, 409 ya completado) + 12 tests nuevos con mocks para el camino feliz de las 5 tablas. 124/124 tests, `ruff check .` limpio. **Épica E6 en curso** (quedan BQI-62/63/64/65 y el orquestador real que conecte las fases)
- [x] BQI-62 — Beat schedule + heartbeat (2026-08-18) — el código (`app/tasks/locks.py`, `app/tasks/heartbeat.py`, `beat_schedule` en `celery_app.py`) ya existía completo desde BQI-05, pero sin ningún test. Agregados `tests/test_locks.py` (5 tests), `tests/test_heartbeat.py`, `tests/test_celery_app.py` (sanidad del `beat_schedule`). **Cambio de diseño real, encontrado al probar contra la cuenta real de Healthchecks.io**: el esquema original (`HEALTHCHECKS_PING_KEY` + auto-provisioning por slug, URL `/ping-key/slug`) devolvía `404 not found` pese a una key válida — probablemente auto-provisioning deshabilitado/de plan pago en esta cuenta. Cambiado a ping directo por UUID de check (`HEALTHCHECKS_CHECKS="slug:uuid,slug:uuid"`, un check creado a mano por tarea, uno por cada tarea de Beat). 135/135 tests, `ruff check .` limpio. Ambos checks ("poll-woo-orders", "poll-sap-invoices") verificados en vivo contra la cuenta real (start+éxito, `200 OK`) — el camino `/fail` no se probó en vivo a propósito (dispararía una alerta real si Felipe tiene notificaciones configuradas), queda cubierto solo por los tests con mock. **Épica E6**: quedan BQI-63 (Sentry + api_logs), BQI-64 (API Key), BQI-65 (RUNBOOK.md) y el orquestador real que conecte las 7 fases
- [x] BQI-63 — Sentry + `api_logs` (2026-08-18) — `app/core/sentry.py`, puerto directo de Stock-Service (ya genérico). Conectado en `app/main.py` (`init_sentry("web")`) y `app/tasks/celery_app.py` (`init_sentry("worker")`, cubre worker y beat). 5 tests nuevos (no-op sin DSN, inicializa con DSN, idempotente, no rompe si el SDK falla). 139/139 tests, `ruff check .` limpio, confirmado que `uvicorn` sigue arrancando bien con el cambio. `SENTRY_DSN` real agregado a `.env` — probado con una excepción real (`ValueError`) capturada y enviada con `sentry_sdk.capture_exception()` + `flush()`, **confirmado visualmente por Felipe en el dashboard**. Sentry cerrado.

**`api_logs` (2026-08-18) — cerrado.** Puerto completo del patrón de Stock-Service: cola en Redis (`app/core/api_log.py::log_api_call`/`drain_api_logs`/`make_response_hook`) + modelo `ApiLog` (migración `25f41acdd5b1`, mismo bug de `import sqlmodel` faltante corregido antes de aplicar) + `app/pipelines/cleanup.py::flush_api_logs` + tarea `task_flush_api_logs` (nueva, cada 5 min, agregada también `_run_async` — primera vez que este proyecto invoca una función async real desde una tarea Celery). Hook enganchado en los 5 clientes HTTP (`SAP`, `TokenSAP`, `WooCommerce`, `Facele`, `StockService`) vía `_http.hooks["response"].append(...)`. Solo Token-SAP-BQ (`/session`/`/session/invalidate`) manda password en el body — confirmado que ningún otro cliente lo hace (Facele/StockService van por header, Woo/SAP por auth/cookies), así que se mantuvo el filtro por path simple de Stock-Service sin necesitar redacción de secretos por valor. Pequeño refactor de paso: `_utc_now_naive()` (antes privada y duplicable en `mixins.py`) pasó a `app/utils/dates.py::utc_now_naive()` compartida, usada también por `ApiLog`/`Failure`. **Verificado en vivo, extremo a extremo**: llamada real a Stock-Service → encolada en Redis → `flush_api_logs` → fila real en Postgres (`id=1`, `StockService`, `200`); llamada real a `/session` de Token-SAP-BQ → confirmado `request_body: null` (password nunca se registra). 152/152 tests, `ruff check .` limpio. **Épica E6**: quedan BQI-64 (API Key), BQI-65 (RUNBOOK.md) y el orquestador real — es más grande (cola en Redis + modelo + tarea Celery de flush + hook en 5 clientes HTTP), se aborda como su propio paso
- [x] **Orquestador manual — `POST /pipeline/sync-order/{code}`** (2026-08-18, no ticketeado — a pedido de Felipe, antes de conectar Beat) — `app/pipelines/orchestrator.py::sync_order_to_sap()` encadena `resolve_customer` → `prepare_billing` → `create_sap_invoice` (por chunk) para UN pedido puntual, trayéndolo de WooCommerce si todavía no está en `woo_orders` (`app/services/woocommerce/orders.py::obtener_pedido()`, nuevo — GET por ID). Nunca lanza sin manejar: devuelve qué fase falló y qué sí se logró; un chunk fallido no bloquea a los demás (I2). 7 tests nuevos con mocks. **Probado en vivo contra SAP TEST real** con pedido `27385`: cliente resuelto (`CN20195519-K`), factura trocaeada, falló la creación en SAP con el error ya conocido de tasa de cambio (`code -10`, ver `memory/project_tasa_cambio_sap.md`) — confirma que el endpoint reporta fallos puntuales sin romperse. Pendiente: definir y armar el segundo endpoint que pidió Felipe, y el flag on/off (Redis) para cuando se conecte Beat.

- [x] **Servicio de carga automática de tasa de cambio** (2026-08-18, no ticketeado — resuelve de raíz el hallazgo de BQI-35/`memory/project_tasa_cambio_sap.md`) — `app/services/sap/exchange_rates.py::asegurar_tasa_cambio(fecha)`. Confirmado contra SAP real (vía `$metadata`) que existen `SBOBobService_GetCurrencyRate`/`SBOBobService_SetCurrencyRate` — **los parámetros van en el body JSON, no en query params**, a pesar de ser FunctionImport (mismo tipo de sorpresa que `U_WedDocNum` en BQI-37: nunca asumir el formato de una llamada nueva a SAP sin probarla). Fuente del valor: mindicador.cl (mismo proveedor que ya usa Stock-Service, aunque ese caso solo lee — nunca escribe en SAP). Cachea la confirmación en Redis 20h para no golpear SAP/mindicador.cl en cada factura del mismo día. Enganchado en `create_sap_invoice` (`app/pipelines/billing.py`), antes de armar el payload — si falla, `FAILED` + `TransientError` con mensaje explícito, sin llegar a tocar el POST de la factura. 8 tests nuevos + 1 de regresión en `test_create_sap_invoice.py`. **Verificado en vivo, dos veces, con pedidos reales de fechas distintas** (`27385`/06-08, `27469`/07-08) que no tenían su tasa histórica cargada — ambos terminaron `COMPLETED` (`DocEntry 103964`/`103966`) sin ninguna carga manual, el servicio la detectó y cargó solo. 169/169 tests, `ruff check .` limpio.
- [x] **Interruptor on/off + Chain A automática conectada a Beat** (2026-08-18, no ticketeado) —
  `app/core/pipeline_state.py` (Redis `pipeline:enabled`, falla CERRADO si Redis cae — al revés que
  `pipeline_lock`, que falla abierto — nunca "procesar a ciegas"). Endpoints `GET/POST /pipeline/status`,
  `/enable`, `/disable`. `app/pipelines/failure_tracking.py::escalar_si_agotado()` — al agotar
  `RESOLVE_CUSTOMER_MAX_ATTEMPTS`/`SAP_BILLING_MAX_ATTEMPTS` (ya existían en `.env`, nunca se usaban),
  sube a `EXHAUSTED`, crea fila en `failures`, dispara `notify_failure` (I6) — enganchado en `/retry` y
  en el batch automático. `procesar_pedidos_pendientes()` (orchestrator.py) reintenta `PENDING` y
  `FAILED` (no solo `PENDING`) en 2 grupos: WooOrder sin trocear, y `SAPBilling` ya troceado pero sin
  factura (para no perder chunks fallidos que quedaron "invisibles" tras el troceo exitoso). Conectado
  en `task_poll_woo_orders`. 186/186 tests, `ruff check .` limpio.

  **Incidente real al probar en vivo (2026-08-18):** con el flag prendido, la primera corrida detectó
  **3031 "pedidos nuevos"** (I3) — `poll_woo_orders()` se llamaba sin `modified_after`, trayendo TODA
  la historia de `processing` de bioquimica.cl, no solo lo reciente. Se frenó a tiempo (`TaskStop`) tras
  procesar 83 pedidos / crear 54 facturas reales en SAP TEST — sin daño real porque es TEST, pero el
  bug era serio: en producción habría reprocesado pedidos que Integrify ya facturó hace meses, cada
  5 minutos, para siempre. **Fix, calcado del patrón de Integrify-Consola** (confirmado con agente:
  Integrify usa `modified_after = hoy - 1 día hábil` recalculado en cada corrida, SIN checkpoint
  persistido — la protección real es el dedup por `code`, que BQI-31 ya tenía): `WOO_POLL_LOOKBACK_DAYS`
  nuevo (default 1, en `.env`), `_ciclo_woo_orders()` calcula `modified_after` con eso. Verificado en
  vivo: WooCommerce devolvió 3031 pedidos sin el filtro, **14 con el filtro**. Los ~3000 pedidos que
  quedaron mal ingeridos en esta base de prueba no se limpiaron — Felipe confirmó que no importa,
  es entorno de prueba, se limpia/recrea la base antes de producción (ver plan de corte más abajo).
- [x] **Ingesta del sitio nuevo vía BioCommerce PRO** (2026-09-01, no ticketeado) — Angelo arregló el
  `permission_callback` que daba 401 en `/wp-json/bio-commerce/v1/orders/{id}/payload` y agregó el
  masivo paginado (`/orders/payload?date_from=&date_to=&page=&per_page=`), confirmado en vivo con 3
  keys distintas antes del fix y 1 después. Payload normalizado trae ya resuelto lo que antes había
  que inferir a mano: `tax_document.sii_code`/`tax_id`/`business_activity_code` y —el punto que
  bloqueaba— `billing_address.comuna_code` (código de comuna real, ej. `CL_114`), separado del
  `state`/`region` legibles para humanos.
  - `app/services/biocommerce/{client,orders}.py` — nuevo. Mismas credenciales que el cliente nativo
    del sitio nuevo (`WOO_NUEVO_KEY/SECRET`), solo cambia el endpoint. Paginación por
    `page`/`per_page`/`pagination.total_pages` (no `$skip` como SAP).
  - `app/pipelines/woo_orders.py` — `_pedido_biocommerce_a_woo_order()` + `poll_biocommerce_orders()`
    reemplazan al adaptador transicional `_pedido_nuevo_a_woo_order()` (leía `meta_data` a mano sobre
    la API nativa de WooCommerce, rompía cada vez que el checkout cambiaba de mecanismo — pasó 2 veces
    en la misma semana). `poll_biocommerce_orders()` queda armada pero **sin conectar a Beat a
    propósito** — conectarla sin un flag de ambiente correría el riesgo de que el `.env` de producción
    (que hoy también tiene `WOO_NUEVO_*` seteado, leftover de pruebas) empiece a mezclar pedidos de
    prueba del sitio nuevo con la ingesta real de producción.
  - `app/pipelines/orchestrator.py` — `sync_order_to_sap_biocommerce()` +
    `_obtener_o_crear_woo_order_biocommerce()`, en paralelo a `sync_order_to_sap()` (sitio actual), sin
    tocarlo — mismo criterio de aislamiento que ya se usa entre `woocommerce`/`woocommerce_nuevo`.
    Nuevo endpoint `POST /pipeline/sync-order-biocommerce/{code}`.
  - Paquete `app/services/woocommerce_nuevo/` **eliminado** — completamente reemplazado, sin usos
    restantes (confirmado por grep antes de borrar).
  - 21 tests nuevos (`test_woo_orders.py`/`test_orchestrator.py`), 207/207 tests, `ruff check .`
    limpio. **Probado en vivo contra SAP TEST real** con el pedido `9232`: `resolve_customer` ahora
    resuelve bien (`CN19720592-K`, comuna Buin/`CL_114` matcheada contra el catálogo real) — se frena
    después en `prepare_billing` con `"sin paid_at"`, correcto: ese pedido de prueba sigue sin pagar de
    verdad, no es un bug. Falta un pedido de prueba pagado con producto real para ver la factura
    completa de punta a punta.
- [x] BQI-64 — Middleware API Key (2026-09-01) — `app/main.py`, portado del mismo patrón que
  Stock-Service: `@app.middleware("http")` verifica `X-API-Key` contra `settings.API_KEY` en todos
  los endpoints salvo `/health` (exento, es el liveness del healthcheck de Docker). Si `API_KEY` está
  vacío, no exige nada (desarrollo local sin key). Guard adicional al arrancar: en producción
  (`ENVIRONMENT=production`) con `API_KEY` vacío, el proceso ni levanta (`RuntimeError` — mejor que
  quede sin arrancar a que quede una API administrativa expuesta sin auth). 5 tests nuevos
  (`tests/test_main.py`, con `TestClient` real — el middleware solo se dispara en el ciclo HTTP, no
  llamando a las funciones de ruta directo como el resto de los tests). 212/212 tests, `ruff check .`
  limpio. `docs/API.md` actualizado.
- [x] **`POST /pipeline/sync-invoice/{doc_entry}`** (2026-09-01, no ticketeado) — faltaba el
  equivalente de `sync-order` para la otra punta del pipeline: hasta hoy Facele/Brevo solo se
  podían probar vía Chain B automática o `/retry` sobre una fila ya existente, sin forma de
  disparar folio→PDF→correo para un `doc_entry` puntual desde cero.
  `orchestrator.py::sync_invoice_to_email()` + `_obtener_o_confirmar_sap_invoice()`: si la
  `SAPInvoice` ya existe la reutiliza; si no, confirma contra SAP que ya tenga folio asignado
  (mismo criterio que `poll_sap_invoices`, "esperar" no es error) antes de crearla, y sigue con
  `_procesar_factura()` (ya existente, reusado tal cual). 4 tests nuevos, 217/217 tests, `ruff
  check .` limpio. `docs/API.md` actualizado.
- [ ] BQI-65 — `RUNBOOK.md`
- [x] Chain B automática (folio → PDF → email) — `task_poll_sap_invoices` conecta
  `procesar_facturas_pendientes` (`app/tasks/scheduled.py::_ciclo_sap_invoices`), mismo patrón que
  Chain A. Probada en vivo contra SAP TEST/Facele producción/Brevo real (54 facturas del incidente de
  polling, ver más abajo). Checkbox quedó desactualizado, corregido acá — el código ya estaba.

### E7 — Pruebas (transversal)
- [ ] BQI-70 — `conftest.py` + fixtures
- [ ] BQI-71 — `test_invariants_coverage.py`
- [ ] BQI-72 — E2E contra mocks
- [ ] BQI-73 — CI

---

## 10.1 Checklist de corte a producción

Nada de esto es urgente durante el desarrollo — son cambios de `.env`/infraestructura, no de código,
diferidos a propósito hasta que se decida ir a producción. Consolidado acá para no perder ninguno:

- [ ] **Base de datos nueva** (2026-08-18) — no reutilizar/limpiar la de desarrollo (quedó con datos
  de prueba, incl. los ~3000 pedidos del incidente de polling). `alembic upgrade head` contra una base
  vacía crea el esquema de cero. Migrar a mano los 4 catálogos estáticos ya validados
  (`municipalities`/`industries`/`delivery_methods`/`bill_document_types`, los 346 municipios de
  BQI-21) vía `pg_dump`/`COPY` desde la base de desarrollo — no reconectar a la MySQL de Integrify.
- [ ] BQI-12 — Alta de `bq-integraciones` en `AUTHORIZED_SERVICES` de Token-SAP-BQ de **producción**
  (hoy usa credenciales de test de `gestor-bq`, ver `memory/project_credenciales_test_vs_prod.md`).
  **Nota (2026-08-28):** verificado en vivo que `token-sap-bq-production.up.railway.app` con
  `gestor-bq`/`gestor-bioquimica-2026` YA responde sesión válida (`sap_db=CLPRDBIOQUIMICA`) — el
  servicio de producción existe y el gestor tiene acceso; falta decidir el momento de usarlo desde acá.
- [ ] Compañía SAP de producción real en `SAP_URL`/sesión (hoy `CLTSTBIOQUIMICA`, confirmado por
  `sap_db` de la sesión de Token-SAP-BQ — ver hallazgo 2026-08-18).
- [ ] Destinatarios reales de Brevo (`BREVO_INVOICE_BCC`/`ALERT_EMAILS`, sacados de Strapi de
  Integrify — ver `memory/project_brevo_destinatarios.md`; hoy todo apunta a felipe.morales@bioquimica.cl).
- [ ] `ENVIRONMENT=production` — sin esto, `send_email()` sigue redirigiendo todo a `ALERT_EMAILS`
  con `[PRUEBA]`, ningún correo llega a un cliente real (freno ya construido y probado, BQI-52).
- [ ] `pipeline_state` arranca apagado por defecto — decidir explícitamente cuándo prenderlo
  (`POST /pipeline/enable`) recién con todo lo anterior confirmado.
- [ ] **Couriers reales en `delivery_methods`** (2026-09-02) — cargar `SGmoveup`/`SGchistn`/
  `SGchiexp` (MoveUp/Chibra Terrestre/Chibra Express) + Starken cuando se defina su SKU (ver R8 y
  registro E3). Sin esto, cualquier pedido con despacho real factura con el SKU genérico `SG000096`
  en vez del courier correcto.

## 10.2 Ambiente de desarrollo desplegado (2026-08-28/09-01)

Repo propio + CI/CD + ambiente `desarrollo` corriendo de punta a punta en el servidor on-prem
(`152.230.53.151`), separado por completo de lo que eventualmente sea producción.

- **Repo:** `github.com/FelipeMv2301/BQ-Integraciones`, privado. Dos ramas: `desarrollo`/`produccion`
  (sin tilde/mayúscula a propósito, evita problemas de nombres de rama entre Windows/Linux en
  scripts/CI). Commit inicial con `.gitignore` (excluye `.env`/`.env.*`, deja pasar `.env.example`) —
  verificado con `git status` antes del primer commit que ningún secreto se coló.
- **`docs/`** — reorganizados ahí todos los ejemplos de payload, `campos-payload-sap.md`,
  `respuesta-payload-bio-commerce.md`, `catalogo-municipalities.csv` (346 filas) y el payload real de
  BioCommerce (`ejemplo-payload-web-nuevo-bio_commerce.json`, pedido `5914`).
- **`bq_integraciones_test`** — base Postgres nueva y limpia en el mismo servidor (`bq_integraciones`
  original queda intacta, con la basura del incidente de polling, sin tocar). `alembic upgrade head`
  desde cero + los 4 catálogos estáticos migrados vía `pg_dump`/`COPY` (346/9/3/9 filas, conteos
  verificados). Requirió agregar reglas nuevas a `pg_hba.conf` (el servidor filtra por nombre de base,
  no hay wildcard) + `pg_reload_conf()`.
- **`.env.desarrollo`** — perfil separado del `.env` real: `WOO_URL/KEY/SECRET` apuntan al sitio de
  prueba (`bioquimica.devwebs.cl`, ya no a `WOO_NUEVO_*`), `DATABASE_URL` a `bq_integraciones_test`,
  `REDIS_URL` con índice `/1` (no `/0`) para que `pipeline_state`/locks/caché SAP no choquen si algún
  día desarrollo y producción corren contra el mismo Redis, `HEALTHCHECKS_CHECKS` vacío a propósito
  (los UUIDs de hoy son los reales de producción — un worker de prueba no debe ensuciar esa señal).
- **CI/CD:** runner self-hosted (`actions-runner-bq-integraciones`, systemd, mismo patrón que
  Gestor-BQ/MELI-BQ) + `.github/workflows/deploy-desarrollo.yml` — push a `desarrollo` dispara
  `alembic upgrade head` + `docker compose up -d --build` en el servidor. `docker-compose.yml` ya traía
  el puerto fijo (`8020:8000`) pensado para este server desde BQI-06, no hizo falta tocarlo. El `.env`
  real vive sin trackear en el workdir del runner (`checkout` con `clean:false` para no borrarlo entre
  corridas). Probado de punta a punta: 2 corridas fallaron limpio sin `.env` (esperado), 3ra corrida
  con `.env` ya puesto: build completo (~10 min primera vez, sin caché) + 3 contenedores arriba +
  `/health` → `200`.
- **URL pública:** `https://bq-integraciones-dev.bioquimica.cl` — agregado al túnel Cloudflare
  compartido (`mirastock/cloudflared/config.yml` + `mirastock/Caddyfile`, mismo tunnel ID que
  mirastock/gestor/meli-dev), reverse proxy a `host.docker.internal:8020`. Requirió reiniciar
  `caddy`/`cloudflared` de mirastock (corte de segundos para esos otros sitios). CNAME en Cloudflare
  (`bq-integraciones-dev` → `<tunnel-id>.cfargotunnel.com`, proxied) creado por Felipe. Verificado
  funcionando end-to-end.
- **`pipeline_state` sigue apagado** — nada se procesa automático en este ambiente hasta prenderlo
  explícito, ni siquiera estando desplegado y con URL pública.

## 11. Resumen del estado del proyecto

*(se completa al cierre de cada sesión de implementación — qué se hizo, qué quedó pendiente, qué se
descubrió que cambia el plan)*

### 2026-09-02 — Prueba end-to-end con pedido de prueba real

Se creó un pedido de prueba real en `bioquimica.devwebs.cl` (ID `9234` — RUT propio de Felipe
`21.269.680-3`, Factura, 2 productos reales con stock, `paid_at` seteado) para verificar la
implementación de punta a punta. Hallazgos de la sesión:

- **Bug real (nuestro, bloqueante)** — `_bc_extraer_paid_at()` (`app/pipelines/woo_orders.py`) no
  manejaba datetimes con offset (`payment.paid_at` de BioCommerce viene con `+00:00`; la columna
  `paid_at` es `TIMESTAMP WITHOUT TIME ZONE`) — rompía el insert en Postgres. Primer pedido de prueba
  que trae `paid_at` real (los anteriores estaban sin pagar), nunca se había ejercitado este camino.
  **Fixeado** (normaliza a UTC naive), `ruff` limpio, 23/23 tests — **pendiente de commit/push** para
  llegar al ambiente `desarrollo` desplegado.
- **Falso bug** (era la data de prueba armada por Claude, no el pipeline ni el plugin) — el pedido de
  prueba se armó primero con meta_data del esquema viejo (`_billing_*`, Integrify) en vez del esquema
  real que usa el checkout hoy (`_bio_*`) — confirmado comparando contra un pedido real de checkout
  (`9232`). `docs/respuesta-payload-bio-commerce.md` (28-ago) quedó **obsoleto**: reporta como bug del
  plugin algo que ya no aplica con el esquema `_bio_*` actual. Corregido el meta_data de `9234`, el
  payload normalizado ahora trae `sii_code`/`business_activity_code`/comuna completos.
- **Gap real, sin cerrar** — SKU de envío por courier (R8, registro E3): hoy cualquier despacho
  factura con el SKU genérico `SG000096`. 3 SKUs ya confirmados por Felipe, Starken pendiente — falta
  cargarlos en `delivery_methods`.

**Pendiente para la próxima sesión**: commit+push del fix de `paid_at`, cargar los couriers en
`delivery_methods`, y volver a disparar `sync-order-biocommerce/9234` contra el servidor real para
completar la prueba de punta a punta (todavía no se llegó a `create_sap_invoice`).

### 2026-09-03 — Auditoría de código, ciclo completo en SAP TEST, y BioCommerce PRO como único origen

**Auditoría de consistencia/eficiencia (2026-09-02, cerrada hoy)** — a pedido de Felipe ("no se
puede tener código así a largo plazo"), 2 frentes en paralelo (patrón de normalización disperso +
code review general) encontraron 16 hallazgos. 7 aplicados uno por uno, cada uno con test de
regresión nuevo, 228/228 tests, 3 commits (`50eb69c`/`75faad8`/`f85ed04`):
- Helper `marcar_fallido()` (`app/pipelines/errors.py`, nuevo) centraliza FAILED+attempts+=1+commit,
  copy-pasteado ~13 veces antes — cerraba el hueco real de llamadas a SAP/Stock-Service sin
  try/except, que dejaban `attempts` sin subir y la entidad reintentándose en silencio para siempre.
- `poll_woo_orders`/`poll_biocommerce_orders` aislados por pedido (I2) — antes uno malformado
  abortaba el lote completo.
- `SAPBilling` huérfanos por discrepancia de totales (validar antes de `session.add`, no después).
- `/retry/woo_orders/{id}` reintenta el ciclo completo (`resolve_customer` + `prepare_billing`), no
  solo lo segundo.
- Copy-paste `SAP_BILLING_MAX_ATTEMPTS`→`RESOLVE_CUSTOMER_MAX_ATTEMPTS` en `_escalar_resolve_customer`.
- Escalamiento inmediato ante `PermanentError` (antes esperaba a agotar `max_attempts` en vano).

3 hallazgos descartados por ser réplica fiel de Integrify-Consola/Stock-Service (no bugs nuestros,
confirmado grepeando el legado antes de tocar nada): `contact_code=contact_name` en
`BillingPayload.build` (`sap/billing.py`), lock sin token de propiedad (`tasks/locks.py`, idéntico en
Stock-Service), caché de sesión SAP que confunde TTL vencido con Redis caído (`sap/session.py`,
idéntico en Stock-Service). El redondeo mixto ya estaba decidido y cerrado desde el 14-ago
(`memory/project_gaps_billing_boleta_decimales_resume.md`).

**MELI-BQ retirado** — era una prueba de una integración con Mercado Libre que no funcionó. Carpeta
local borrada, servicio systemd roto (`actions.runner.FelipeMv2301-MELI-BQ...`, apuntaba a un
directorio de runner que ya no existía) parado/deshabilitado/borrado del servidor. Repo de GitHub
queda intacto.

**Primer ciclo completo, punta a punta, en SAP TEST real** — pedido de prueba `9237`
(`bioquimica.devwebs.cl`, RUT propio de Felipe, datos limpios desde el inicio con el esquema
`_bio_*` correcto) llegó `COMPLETED` (`DocEntry 104019`, `DocNum 30402`). En el camino, 2 hallazgos
de **datos maestros de SAP TEST** (no bugs de código, confirmados consultando `Items()` directo en
Service Layer): el SKU `RP0436B3` tenía `SalesItem=tNO` (activo pero no marcado para venta) y el SKU
`RP0107B1` tenía stock en bodega `11` pero el pipeline factura contra la `01` que resuelve
Stock-Service — Felipe corrigió ambos en SAP directamente.

**Hallazgo urgente al prender `pipeline_state`** — Felipe pidió prender el interruptor para probar
reintentos automáticos. `task_poll_woo_orders` (Beat) todavía llamaba al adaptador **nativo** de
WooCommerce (`poll_woo_orders` viejo), no al de BioCommerce PRO — la API nativa de
`bioquimica.devwebs.cl` no trae `tax_id`/`document_type` dentro de `billing` (confirmado contra un
pedido real), así que cualquier pedido real nuevo se habría ingerido sin RUT ni comuna resueltos y
fallado de entrada en `resolve_customer`. Apagado a los pocos minutos, sin daño (`/failures` vacío,
ningún ciclo alcanzó a correr). Causa: `poll_biocommerce_orders()` se había dejado **a propósito**
sin conectar a Beat (1-sep) porque en ese momento bioquimica.cl era "el sitio real" y devwebs.cl "el
nuevo, en pruebas" — la situación se invirtió (devwebs.cl es la web real ahora) y el código nunca se
actualizó.

**Consolidación a BioCommerce PRO como único origen** (a pedido explícito de Felipe: "no mapeemos
por diferentes variables de entorno... quiero que todo el código esté adaptado a la estructura de
BioCommerce PRO") — retirado por completo el path nativo de WooCommerce:
- `app/services/woocommerce/` (client.py + orders.py) **borrado**.
- `app/pipelines/woo_orders.py`: quedó solo el adaptador BioCommerce, renombrado a los nombres
  canónicos (`_pedido_a_woo_order`, `poll_woo_orders`, sin prefijo `_bc_`/sufijo `_biocommerce` — ya
  no hay ambigüedad de cuál es cuál).
- `orchestrator.py`: `sync_order_to_sap_biocommerce`→`sync_order_to_sap`, ídem
  `_obtener_o_crear_woo_order`. Endpoint único `POST /pipeline/sync-order/{code}` (se retiró
  `/sync-order-biocommerce/{code}`).
- `task_poll_woo_orders` ahora sí llama al poller de BioCommerce — cierra el hallazgo urgente de
  arriba.
- `config.py`: una sola `WOO_URL`/`WOO_KEY`/`WOO_SECRET` (se retiró `WOO_NUEVO_*`) — el sitio activo
  se cambia ahí, no mapeando variables por sitio. Asume BioCommerce PRO instalado en lo que sea que
  apunte.
- `.env`/`.env.desarrollo`/`.env` del servidor actualizados. **Ojo**: el `.env` raíz (no
  `.desarrollo`) seguía apuntando a `bioquimica.cl` (sin BioCommerce PRO) — queda con una nota de
  advertencia, no se cambió el valor sin confirmar con Felipe. El `.env.desarrollo` local y el del
  servidor tenían además **2 keys distintas** de Woo para devwebs.cl bajo `WOO_KEY` vs
  `WOO_NUEVO_KEY` — la vieja (`ck_df04...`/servidor `ck_8185...`) da 401 al escribir; se consolidó a
  la key con permiso Lectura/Escritura confirmada en vivo toda la sesión (`ck_2b4881e2...`).
- 216/216 tests (bajó de 228 al sacar los del path nativo, no son bugs), `ruff check .` limpio.

**Pendiente**: prender `pipeline_state` de nuevo ahora que Beat usa el poller correcto (quedó
apagado tras el hallazgo urgente) — decidir el momento con Felipe. Courier SKU (R8) sigue sin
cerrar.
