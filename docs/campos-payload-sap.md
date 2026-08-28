# Campos que se envían a SAP Business One

Referencia completa de los dos payloads que arma BQ-Integraciones antes de escribir en SAP —
heredados tal cual de Integrify-Consola (ningún valor fijo se inventó nuevo acá). "Fijo" = mismo
valor siempre, sin importar el pedido/cliente. "Dinámico" = varía según el pedido/cliente real.

Fuente: `app/services/sap/customers.py::CustomerPayload` y
`app/services/sap/billing.py::BillingPayload`.

---

## 1. Payload de Cliente (Business Partner) — `POST/PATCH BusinessPartners`

| Campo SAP | Viene de | Tipo |
|---|---|---|
| `CardCode` | código del cliente (`CN{rut}` o el que ya tenga en SAP) | dinámico |
| `CardName` | nombre/razón social | dinámico |
| `FederalTaxID` | RUT | dinámico |
| `U_NX_GIRO` | giro (texto libre) | dinámico |
| `Phone1` | teléfono | dinámico |
| `EmailAddress` | email | dinámico |
| `ContactPerson` | nombre del contacto | dinámico |
| `U_ActEconomica` | código de industria SII (catálogo de 9 giros) | dinámico |
| `CardType` | `"C"` | **fijo** |
| `GroupCode` | `"100"` | **fijo** |
| `SalesPersonCode` | `"25"` (vendedor Web) | **fijo** |
| `DebitorAccount` | `"1140101"` | **fijo** |

### `BPAddresses[]` — 2 filas siempre (facturación + despacho)

| Campo SAP | Viene de | Tipo |
|---|---|---|
| `AddressType` | `bo_BillTo` / `bo_ShipTo` | fijo por fila |
| `AddressName` | `FISCAL`/`DESPACHO` (o nombre libre siguiente si ya existen) | dinámico |
| `AddressName2` | `"WEB"` — marca de origen, para reconocer/reutilizar la dirección propia | **fijo** |
| `Street` | dirección | dinámico |
| `County` | comuna | dinámico |
| `City` | ciudad | dinámico |
| `State` | región | dinámico |
| `Country` | `"CL"` | **fijo** |
| `TaxCode` | `"IVA"` | **fijo** |

### `ContactEmployees[]` — 1 fila

| Campo SAP | Viene de | Tipo |
|---|---|---|
| `Name`, `FirstName`, `LastName` | datos del contacto (dirección de despacho) | dinámico |
| `MobilePhone`, `E_Mail` | teléfono/email del contacto | dinámico |
| `Remarks1` | `"WEB"` — misma marca de origen | **fijo** |

---

## 2. Payload de Factura/Boleta — `POST Invoices`

| Campo SAP | Viene de | Tipo |
|---|---|---|
| `U_IX_Ind` | tipo de documento (`33` Factura / `39` Boleta) | dinámico |
| `CardCode` | código del cliente ya resuelto | dinámico |
| `CntctCode`, `ShipToCode`, `PayToCode` | contacto/direcciones del cliente | dinámico |
| `DocDate`, `TaxDate`, `DocDueDate` | **misma fecha las 3** — R1: si `DocDueDate` difiere de `DocDate`, el DTE imprime "Crédito" en vez de "Contado" | dinámico (la fecha en sí), pero las 3 iguales **a propósito** |
| `DocTotal` | total del chunk | dinámico |
| `U_WedDocNum` | número de pedido Woo — es la referencia que relaciona el chunk con el pedido en SAP | dinámico |
| `Comments` / `U_NX_Observacion` | notas internas/públicas (`"Pedido web {reference}"`) | dinámico |
| `U_BQ_CodVoucher` | código de autorización de pago (opcional, puede venir `null`) | dinámico |
| `PaymentGroupCode` | `-1` (contado) | **fijo** |
| `SalesPersonCode` | `"25"` | **fijo** |
| `U_BQ_AREA` | `"TRA"` | **fijo** |
| `U_VentaWeb` | `"Y"` | **fijo** |
| `U_TipoVenta` | `"WEB"` | **fijo** |
| `U_IXP_EXCLUDED` | `"N"` | **fijo** |
| `U_Traspaso_FE` | `"N"` | **fijo** |
| `U_IndTraslado` | `"1"` | **fijo** |
| `U_TipoDesp` | `"2"` | **fijo** |
| `DocumentSubType` | calculado: `bod_None` si Factura (`33`), `bod_Bill` si Boleta | dinámico (derivado del tipo de documento) |

### `DocumentLines[]` — una por ítem, hasta 21 por chunk (R2)

| Campo SAP | Viene de | Tipo |
|---|---|---|
| `ItemCode` | SKU (resuelto vía Stock-Service) | dinámico |
| `Quantity`, `UnitPrice`, `LineTotal` | cantidad y montos del ítem | dinámico |
| `WarehouseCode` | bodega (resuelta vía Stock-Service) | dinámico |
| `DiscountPercent` | `0` | **fijo** |
| `CostingCode` | `"CG3060"` | **fijo** |
| `CostingCode2` | `"CC1000"` | **fijo** |
| `VatGroup`, `TaxCode` | `"IVA"` | **fijo** |
