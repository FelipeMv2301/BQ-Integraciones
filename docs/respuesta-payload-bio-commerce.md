# Respuesta técnica — payload de pedidos (Bio Commerce PRO)

Contexto: revisamos el payload normalizado (`GET /wp-json/bio-commerce/v1/orders/{id}/payload`) contra
lo que **BQ-Integraciones** necesita para facturar en SAP Business One, comparándolo también contra la
API REST nativa de WooCommerce del mismo pedido de prueba (`5914`).

---

## 1. ¿La estructura y los nombres de campos son adecuados para SAP?

En general sí — el payload normalizado es más ordenado que trabajar contra la API cruda de WooCommerce.
Encontramos 3 puntos a ajustar antes de darlo por cerrado:

- **`tax_document.business_activity` viene `null` en el ejemplo, pero el dato SÍ existe.** En la API
  REST nativa de WooCommerce, el mismo pedido trae `meta_data._billing_business_activity = "giro
  prueba"`. Hay una desincronía en la transformación del plugin — ese campo no se está copiando al
  payload normalizado. Vale la pena revisar esa parte del mapeo.

- **Falta el código de giro/industria (SII).** En la API nativa existe como
  `meta_data._billing_industry` (`"DIST"` en el ejemplo de prueba), pero no aparece en ningún campo
  del JSON normalizado — ni en `tax_document` ni en `customer`. Es un dato que SAP necesita para
  clasificar al cliente (mapeamos este código a nuestro catálogo interno de giros). Sugerimos
  agregarlo, por ejemplo `tax_document.industry_code`.

  **Nuestro catálogo de giros** (el que ya usamos, sin cambios respecto al sitio actual —
  confirmamos que `"DIST"` del ejemplo y `"SILVO"` de otro pedido de prueba ya calzan con esta
  tabla, así que del lado de los códigos en sí no habría que tocar nada, solo exponerlo en el
  payload):

  | Código (`woo_code`) | Nombre | Código SAP |
  |---|---|---|
  | `EDUCA` | Educación | `AE1` |
  | `SALUD` | Salud humana | `AE2` |
  | `DIST` | Distribuidora y Comercializadora | `AE3` |
  | `SILVO` | Silvoagropecuario | `AE4` |
  | `CONST` | Construcción | `AE5` |
  | `INDUS` | Industrial | `AE6` |
  | `ALIM` | Alimentos | `AE7` |
  | `PART` | Particular | `AE8` |
  | `INVES` | Investigación | `AE9` |

  Si el checkout nuevo agrega alguna opción de giro que no esté en esta lista, avisar antes de
  producción — un código sin mapear haría fallar la resolución del cliente en SAP para ese pedido.

  **¿Es obligatorio?** Depende del tipo de documento: en **Boleta** no aplica (documento a consumidor
  final, sin datos de giro del receptor); en **Factura**, el giro del receptor es parte de la
  identificación estándar del comprador ante el SII — para Factura conviene tratarlo como obligatorio.
  (Esto es el giro del **receptor**, es decir del cliente que compra — el giro del emisor,
  bioquimica.cl, es fijo y ya está configurado en SAP, no depende del pedido.)

- **Falta la fecha de confirmación de pago.** El payload solo trae `order.created_at`/`updated_at`
  (cuándo se creó/modificó el pedido), pero para nosotros la fecha relevante es **cuándo se confirmó
  el pago** — no es lo mismo si un pedido queda `on-hold` (transferencia pendiente) unos días antes de
  confirmarse. Esa fecha es la que usamos como fecha del documento en SAP, y además determina qué tasa
  de cambio USD del día aplica (SAP la exige por la fecha exacta del documento, no la de "hoy").
  Sugerimos agregar algo como `payment.paid_at` (timestamp, `null` mientras no esté pagado).

## 2. ¿Los montos en pesos chilenos deben ir redondeados sin decimales?

**Sí, sin decimales.** CLP no tiene centavos — todo lo que le mandamos a SAP son enteros. Vimos que
`totals.*` ya viene así (`"total": 14489`, limpio), pero `products[].unit_price/subtotal/
total_before_tax/total_including_tax` vienen con decimales completos (ej. `8394.957983`) — eso es
precisión que WooCommerce calcula internamente, pero no existe en la venta real (ni en el cobro, ni en
el documento tributario).

Si necesitan mantener esa precisión para otros fines (reportes internos, etc.), lo ideal sería agregar
un campo entero AL LADO (ej. `unit_price_rounded`) en vez de reemplazar el que ya está — pero para nuestro
consumo, con que `totals.*` siga viniendo entero (como ya está) alcanza; los montos por producto los
recalculamos igual del lado nuestro antes de facturar.

## 3. ¿Qué códigos deben usarse para boleta y factura?

Preferimos el **código SII estándar, directo**: `"33"` = Factura Electrónica, `"39"` = Boleta
Electrónica. Es el que ya usa el sitio actual (bioquimica.cl) y el que nuestro catálogo interno
entiende sin traducción (mapeo 1:1 código SII → código SAP).

Notamos que la API nativa de WooCommerce trae `meta_data._billing_doc_type = "BE"` — un código interno
del sistema anterior (Integrify), no el código SII. Si el payload normalizado va a exponer ese mismo
código interno en `tax_document.type`, necesitamos la tabla de equivalencia **completa** (todos los
valores posibles: factura, boleta, y cualquier variante exenta que exista) para poder traducirlo de
nuestro lado. Si es posible que manden directamente el código SII (`"33"`/`"39"`) en `tax_document.type`,
nos evitamos ese paso de traducción y una fuente más de error.

## 4. ¿`external_id` cumple el formato esperado para relacionar el pedido?

No hay un formato estricto que necesitemos de nuestro lado — solo que sea:
- **Único por pedido** (nunca se repite entre pedidos distintos).
- **Estable** (no cambia si el pedido se actualiza después).

`external_id: "bioquimica-1-5914"` (`bioquimica-{customer_id}-{woocommerce_order_id}`) cumple ambas
condiciones. El `woocommerce_order_id` (`5914`) por sí solo también nos alcanzaría — el prefijo con el
`customer_id` es redundante para nuestro uso, pero no genera ningún problema si les sirve para otros
propósitos internos. Pueden dejarlo como está.

## 5. ¿Qué campos adicionales requiere SAP?

Resumen de lo que falta o hay que confirmar (ya cubierto en los puntos anteriores):

- Código de giro/industria SII (punto 1) — falta.
- Fecha de confirmación de pago (punto 1) — falta.
- `business_activity` (punto 1) — existe pero no se está copiando al payload normalizado (bug).
- Código de courier cuando hay despacho (punto 6, abajo) — falta confirmar formato.

El resto (cliente, dirección, productos, montos, medio de pago) ya cubre lo que necesitamos.

## 6. ¿Cómo debe informarse el courier cuando exista despacho?

Hoy, con el sitio actual, identificamos el método de envío por `shipping_lines[0].method_id` (ej.
`"flat_rate"`) — un código de **método de envío de WooCommerce**, que nosotros mapeamos a un SKU de
despacho propio en SAP vía nuestro catálogo interno.

En el payload normalizado, `shipping.courier_code` es el lugar correcto para esto — pero necesitamos
que confirmen **qué va a venir ahí exactamente**:
- ¿El mismo código de método de WooCommerce (`flat_rate`, `local_pickup`, etc.), igual que hoy?
- ¿O un código de courier real (Chilexpress, Correos de Chile, Starken, etc.)?

Si es lo segundo, necesitamos la tabla completa de couriers posibles para mapear cada uno a nuestro
código de despacho en SAP — hoy esa relación no existe en nuestro catálogo.

## 7. ¿Dónde y cómo se recibirá el identificador asignado por SAP?

Esta pregunta es en realidad para **nosotros** (BQ-Integraciones), no algo que SAP defina — y es un
punto real que hay que diseñar en conjunto, todavía no existe.

**Primero, qué identificador es:** cuando creamos la factura en SAP, el sistema devuelve un `DocEntry`
(ID interno de SAP para ese documento). Más adelante, cuando el SII asigna el folio del DTE, SAP
también expone un `FolioNumber`/`DocNum` (el número que ve el cliente final en su boleta/factura). Son
dos identificadores distintos — hay que definir cuál quieren guardar en `sap_reference.sap_document_id`
(o si quieren ambos).

**Segundo, cómo se los enviamos:** hoy BQ-Integraciones solo **lee** pedidos desde WooCommerce — no
tiene ningún mecanismo para escribirles de vuelta. Dos formas de resolverlo:

- **(Recomendada) Webhook saliente nuestro:** nosotros les pegamos a un endpoint que ustedes expongan
  (ej. `POST /wp-json/bio-commerce/v1/orders/{id}/sap-reference`) con el `DocEntry`/folio apenas lo
  tengamos confirmado. Más simple para ustedes — no necesitan consultarnos, nosotros avisamos.
- **Alternativa:** ustedes consultan un endpoint nuestro. Hoy no existe uno pensado para esto
  puntualmente (tenemos `/status`, `/failures` para monitoreo general) — habría que construir uno
  específico tipo `GET /pedidos/{external_id}/estado`.

Preferimos la primera opción. Falta definir con ustedes: el identificador exacto a mandar (`DocEntry`
vs folio final), el endpoint que expondrían para recibirlo, y en qué momento avisamos (¿apenas se crea
la factura en SAP sin folio todavía, o recién cuando el folio ya está confirmado?).

---

## Resumen — qué falta cerrar antes de producción

1. Agregar código de giro/industria SII al payload.
2. Agregar fecha de confirmación de pago.
3. Corregir el bug de `business_activity` (no se copia desde WooCommerce al payload normalizado).
4. Confirmar/simplificar el código de tipo de documento a código SII directo (`"33"`/`"39"`).
5. Definir formato del courier en `shipping.courier_code` (código de método propio vs courier real).
6. Diseñar el mecanismo de respuesta (webhook saliente nuestro hacia un endpoint de ustedes) para
   informar el `DocEntry`/folio de SAP una vez creado.
