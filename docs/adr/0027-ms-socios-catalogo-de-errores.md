# ADR-0027: MS Socios — catálogo de errores, traducción y circuit breaker

- **Estado:** Aceptado (HU34, Sprint 3)
- **Depende de:** [ADR-0025](0025-ms-socios-fachada-oauth-cuota.md) (la fachada de socios y sus errores de acceso) y
  [ADR-0026](0026-ms-socios-venta-embebida.md) (la venta embebida y su traducción provisional)
- **Actualiza:** ADR-0025 (códigos 401 `INVALID_TOKEN` y 503 `SERVICE_UNAVAILABLE`) y reemplaza la traducción provisional de ADR-0026 §8

## Contexto

HU32 y HU33 respondieron los errores de `/partners/v1` con un catálogo provisional: la validación salía como 422, los conflictos de
negocio como 409 `IDEMPOTENCY_CONFLICT`, y el motivo iba en `details` con dos formas distintas. Las rutas inexistentes respondían en el
formato de Starlette, el 500 perdía el `trace_id` (el manejador de `Exception` corre fuera del middleware de correlación) y Socios no
tenía circuit breaker: con una dependencia caída, cada solicitud esperaba el tiempo completo antes de fallar. Suscribir encadena
Identidad (3 s) y Suscripción (10 s): hasta 13 s, más de los 10 s que la guía le pide esperar al socio.

HU34 consolida el catálogo y centraliza la traducción. Solo cambia `services/socios`; Cotización, Identidad y Suscripción no se tocan.

## Decisión

1. **Catálogo de 10 códigos**, los 8 de la guía §4.5 más `INVALID_TOKEN` (401, de HU32) y `SERVICE_UNAVAILABLE` (503, de HU32). La fuente
   única es `app/socios/errores.py` (`CATALOGO`), y el OpenAPI lo publica como `enum`.
2. **Negocio y validación son 400 `INVALID_REQUEST`.** La validación pasa de 422 a 400. Las reglas de negocio (cliente no apto,
   cotización usada o vencida, firma vencida, OTP inválido...) también son 400, con un `reason` de una lista cerrada. **409
   `IDEMPOTENCY_CONFLICT` queda solo para la clave de idempotencia.**
3. **`error.reason`** es un campo opcional nuevo con la lista cerrada de motivos (`MOTIVOS`, también `enum` en el OpenAPI); `details`
   solo lleva campos inválidos. Es una extensión del criterio 1: `code`, `message` y `trace_id` siguen siempre presentes.
4. **Traducción centralizada** en `app/socios/traduccion.py`, con una tabla por servicio. Los mensajes son propios: nunca se reenvía el
   `message`, los `details` internos ni el `trace_id` de un servicio interno. Un 4xx o `code` sin traducción es una falla de configuración
   nuestra (por ejemplo, una clave de servicio rechazada): 500 `INTERNAL_ERROR` y se registra. Una prueba falla si Suscripción agrega un
   `code` alcanzable sin traducción explícita.
5. **`trace_id`.** La respuesta lleva el de la solicitud en Socios, que es el que el socio reporta. El log `error_traducido` registra
   `trace_id_origen` (el del servicio interno), `servicio`, `status_origen` y `code_origen`, así que con el `trace_id` del socio se llega al log
   interno sin exponerlo. Propagar un solo `trace_id` entre servicios exigiría tocar el middleware de todos y queda fuera.
6. **El 500 se resuelve dentro de `MiddlewareCorrelacion`**, mientras el contexto sigue vivo: el `trace_id` del cuerpo es el del log y la
   respuesta lleva `X-Correlation-Id`. El manejador de `Exception` queda como respaldo.
7. **Circuit breaker por dependencia** (Cotización, Identidad, Suscripción), en memoria de cada pod, con el patrón de ADR-0013. Cuentan
   como fallo un timeout, un error de conexión y un 5xx (incluido el 503 `DEPENDENCIA_NO_DISPONIBLE` de Suscripción); un 4xx no. Tras
   `SOCIOS_CIRCUITO_UMBRAL_FALLOS` (5) fallos seguidos se abre; con el circuito abierto, 503 `SERVICE_DEGRADED` con `Retry-After` sin llamar a
   la dependencia. Vencido `SOCIOS_CIRCUITO_ENFRIAMIENTO_S` (30), la primera solicitud es el sondeo: un éxito cierra, un fallo reabre. Una
   cotización degradada por Cotización (`degradado: true`) sigue siendo 201. El aviso del 503 de Cotización dice que la cotización puede
   volver con un perfil por defecto.
8. **Timeouts.** `SOCIOS_TIMEOUT_SUSCRIPCION_S` baja de 10 a 6 s: Identidad + Suscripción (3 + 6) queda por debajo de los 10 s del socio.
9. **409 «en proceso».** Una solicitud cuya original sigue en curso responde 409 con `reason: SOLICITUD_EN_PROCESO` y `Retry-After: 2`. Es el
   único 409 que el socio reintenta, con la misma clave.
10. **`/oauth/token` sigue en formato RFC 6749** (ADR-0025): es la única excepción al formato del catálogo.
11. **HTTP fuera del catálogo.** Un método no permitido responde 405 con `INVALID_REQUEST` y la cabecera `Allow`; una ruta inexistente,
    404 `RESOURCE_NOT_FOUND`.

La traducción:

| Origen | Respuesta de Socios |
|---|---|
| Cotización 400/422 | 400 `INVALID_REQUEST` con los `details` de campo |
| Cotización 404 `COTIZACION_NO_ENCONTRADA` | 404 `RESOURCE_NOT_FOUND` |
| Identidad 404 | 400 `INVALID_REQUEST`, `reason: CLIENTE_NO_REGISTRADO` |
| Identidad 422 | 400 `INVALID_REQUEST`, `details` del campo `cliente` |
| Suscripción 404 (`*_NO_ENCONTRADA`) o 403 `ACCESO_DENEGADO` | 404 `RESOURCE_NOT_FOUND` (aislamiento, ADR-0026 §6) |
| Suscripción 422 `VALIDATION_ERROR` | 400 `INVALID_REQUEST` con `details` |
| Suscripción 409 `CLAVE_IDEMPOTENCIA_REUTILIZADA` | 409 `IDEMPOTENCY_CONFLICT` |
| Suscripción 409 de negocio o 403 `OTP_INVALIDO` | 400 `INVALID_REQUEST`, `reason` = código de origen |
| 401 (clave de servicio rechazada) o 4xx sin traducción | 500 `INTERNAL_ERROR` |
| 5xx, timeout o conexión rechazada | 503 `SERVICE_UNAVAILABLE` (cuenta como fallo del circuito) |
| Circuito abierto | 503 `SERVICE_DEGRADED` con `Retry-After` |

## Consecuencias

- **Cambio incompatible dentro de `/partners/v1`**, que según la regla de versionado pediría una v2: 422→400, los 409 de negocio a 400,
  el motivo de `details` a `reason` y el timeout de Suscripción. Se acepta porque aún no hay socios reales.
- El catálogo tiene 10 códigos y el campo `reason`; la guía de integración (§4.5, 8 códigos) queda como entregable cerrado y
  [`errores-y-reintentos.md`](../../services/socios/docs/api/errores-y-reintentos.md) es la referencia.
- El circuito es por pod: con varias réplicas, cada una abre el suyo. No se comparte en Redis para no sumar latencia ni otro punto de falla.
- Un 503 de Suscripción abre el circuito hacia Suscripción, de modo que una caída de Cotización detrás de Suscripción también lo abre.
- Si iniciar la saga tarda más de 6 s, el socio recibe 503 aunque la saga siga en curso; reintentar con la misma clave lo resuelve
  gracias a la idempotencia en dos capas (ADR-0026 §7). Hay que medir el tiempo en staging.
- Los mensajes son solo en español; no se localiza según `Accept-Language`.
- Queda fuera: el reporte de consumo por socio (ADR-0026) y el contrato Pact (HA07); el contrato de esta HU es la validación de las
  respuestas de error contra el esquema `ErrorOut` del OpenAPI publicado.
