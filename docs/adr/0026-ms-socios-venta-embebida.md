# ADR-0026: MS Socios — venta embebida sobre la saga de suscripción

- **Estado:** Aceptado (HU33, Sprint 3)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (Socios no guarda datos de negocio),
  [ADR-0012](0012-ms-suscripcion-saga-decision-y-outbox.md) (la saga), [ADR-0019](0019-ms-suscripcion-firma-electronica-y-compensacion.md)
  (firma, cobro antes de firmar y reembolso),
  [ADR-0020](0020-ms-suscripcion-venta-asistida.md) y [ADR-0021](0021-ms-suscripcion-certificado-y-correos-del-asesor.md) (el
  precedente de actuar a nombre del cliente) y [ADR-0025](0025-ms-socios-fachada-oauth-cuota.md) (la fachada de socios)

## Contexto

Con HU32 el socio se autentica y cotiza. HU33 cierra la venta dentro de su canal: inicia la saga de HU15, el cliente paga y firma
sin salir de la app del socio y el socio consulta el resultado. Hoy `POST /v1/suscripciones` autentica al cliente final con su
token, y el socio no lo tiene. El patrón ya existe en la venta asistida del asesor: rutas propias sobre los mismos servicios, sin
duplicar reglas.

La historia dejó tres puntos «a confirmar» (criterios 5, 6 y 8). Se decidieron el 2026-11-19.

## Decisión

1. **Rutas internas sobre los mismos servicios.** Suscripción agrega `/v1/interno/socios` (interno en el borde, regla V15) con la clave
   de servicio que ya usan Notificaciones y Socios. Llaman a `ServicioSuscripcion` y `ServicioFirma`; no se duplican la decisión,
   el cobro, la póliza ni la firma (criterio 2). Socios sigue sin estado de negocio: autentica, aplica idempotencia, identifica al
   cliente y traduce.
2. **Cliente existente (criterio 5).** El socio envía `cliente: {tipo_documento, numero_documento}` en la cotización y en la suscripción.
   - Socios lo resuelve con la nueva ruta interna de Identidad `POST /v1/interno/clientes/identificacion` y exige que exista, con KYC
     `VERIFICADO` y el consentimiento Open Finance vigente. Si falla algo responde 422 con el motivo en `details`
     (`CLIENTE_NO_REGISTRADO`, `KYC_NO_VERIFICADO`, `CONSENTIMIENTO_NO_VIGENTE`).
   - El documento va en el cuerpo y no en la URL, para que no quede en los registros de acceso; la respuesta no lleva datos personales.
   - Sin `cliente`, la cotización es informativa (el `cliente_id` es el uuid5 de HU32) y no se puede suscribir: su titular no coincide.
   - **Dar de alta clientes nuevos desde el socio queda fuera** (gap).
3. **Pago y firma (criterio 6).**
   - **Pago:** el socio tokeniza y envía `metodo_pago`, un token de la pasarela (simulado). Un PAN se rechaza con la misma regla de la venta
     asistida (SAQ-A, ADR-0014).
   - **Firma:** rutas de Socios sobre `ServicioFirma` con modalidad `SOCIO`. El cliente lee el documento, recibe el OTP y firma dentro de la
     app del socio. El firmante sigue siendo el cliente; la evidencia guarda además el socio de origen, y la IP y el dispositivo que reenvía
     el socio.
4. **Consulta, sin aviso (criterio 8).** `GET /partners/v1/polizas/{id}` recibe el `id` que devolvió el `POST` (la relación saga–póliza es
   1:1) y devuelve el estado de la saga y la póliza, con `documento_estado` para saber cuándo se puede firmar (el PDF se genera después del cobro; antes, el documento responde 409 `POLIZA_NO_LISTA`). El socio consulta; **el webhook queda como gap** y mantiene a Socios
   sin estado de negocio (HU32, criterio 9). El cliente recibe el certificado y la confirmación de HU22 por su cuenta.
5. **Atribución (criterio 7).** `suscripciones.socio_client_id` (el `client_id` OAuth) y `firmas.socio_client_id` guardan quién vendió, para
   conciliación y auditoría. La tabla `auditoria_socio` registra cada acción que cambia algo o entrega el documento (inicio, descarga, código
   y firma), también las rechazadas, solo con el código del error y sin PII, OTP ni token de pago. Es inmutable (disparador, como
   `auditoria_asesor`). Consultar no se audita: el socio lo repite hasta ver la póliza.
6. **Aislamiento (criterio 10).** Toda ruta de Suscripción filtra por `socio_client_id`. Una venta de otro socio, o que no es de ningún socio,
   responde 404 igual que una inexistente; no se revela que existe.
7. **Idempotencia en dos capas (criterio 3).**
   - Socios: `X-Idempotency-Key` en Redis (HU32). Misma clave y mismo cuerpo, misma respuesta; otro cuerpo, 409 `IDEMPOTENCY_CONFLICT`.
   - Suscripción: la clave de la saga es `socio:` + sha256(`client_id`, clave). Va a `uq_susc_cliente_clave`, así la garantía también vale
     en la base y si Redis se vacía. Dos socios con la misma clave no se confunden.
8. **Errores (criterio 9), traducción provisional.** HU34 define el catálogo definitivo y aún no existe.

   | Origen | Respuesta de Socios |
   |---|---|
   | 404 | 404 `RESOURCE_NOT_FOUND` |
   | 409 (`COTIZACION_YA_USADA`, `CLAVE_IDEMPOTENCIA_REUTILIZADA`, `FIRMA_VENCIDA`...) | 409 `IDEMPOTENCY_CONFLICT`, con el motivo en `details` |
   | 400, 403 (`OTP_INVALIDO`) o 422 | 422 `INVALID_REQUEST`, con el motivo o los detalles |
   | Clave de servicio rechazada, caída o 5xx | 503 `SERVICE_UNAVAILABLE`, sin detalle interno |

   Se usan los códigos que ya tiene HU32 y el motivo de negocio va en `details`, para no inventar códigos que HU34 pueda descartar.
9. **Contrato nuevo, versión v1.** Tres endpoints en el contrato del socio: `suscripciones:crear`, `suscripciones:firmar` y
   `polizas:consultar`. Un socio con contrato anterior no los tiene hasta que se actualice.

## Consecuencias

- La venta embebida reutiliza la saga completa: lo que cambie en la decisión, el cobro o la firma vale también para los socios.
- **Riesgo aceptado: el OTP es simulado y fijo.** El socio podría firmar sin el cliente. `acepta_terminos` lo declara el socio y, como en
  ADR-0020, no prueba la aceptación; la prueba es la firma con OTP. Debe revisarse con un proveedor de firma real.
- **El cobro ocurre antes de la firma** (ADR-0019): si el cliente abandona en la app del socio, se reembolsa a las 24 h.
- **La cotización no tiene canal «socio».** Cualquier socio que conozca una cotización y el documento del cliente podría suscribirla; la
  atribución vive en Suscripción y el reporte de consumo por socio queda para HU34.
- **Claves de servicio compartidas:** Socios reutiliza el secreto de Suscripción y el de Identidad (listas separadas por coma); no hay una
  clave propia por servicio que llama.
- **`/partners/v1/status` no incluye aún a Identidad ni a Suscripción:** un socio que solo cotiza no depende de ellas.
- Con la firma vía API no se envía el correo de firma de HU39; el correo de confirmación de `poliza_emitida` (HU22) sigue por su evento.
- El estado de la venta se mira consultando; el webhook y el alta de clientes nuevos son los siguientes pasos.
