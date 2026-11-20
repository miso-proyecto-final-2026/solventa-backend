# Errores y reintentos de la API de socios

Referencia de los errores de `/partners/v1` y de cómo reintentar (HU34, [ADR-0027](../../../../docs/adr/0027-ms-socios-catalogo-de-errores.md)).
El contrato máquina-legible está en [`openapi.json`](openapi.json) (esquema `ErrorOut`). Este documento es la referencia del catálogo:
reemplaza a los 8 códigos de la guía de integración §4.5, que no incluía `INVALID_TOKEN` ni `SERVICE_UNAVAILABLE` ni el campo `reason`.

## Estructura del error

Toda respuesta de error de `/partners/v1` es JSON con esta forma:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "El cliente no ha verificado su identidad (KYC) en Solventa.",
    "trace_id": "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8",
    "reason": "KYC_NO_VERIFICADO",
    "details": []
  }
}
```

| Campo | Siempre | Qué es |
|---|---|---|
| `code` | Sí | Código del catálogo. Es **estable**: programe contra él. |
| `message` | Sí | Texto en español para una persona. **Puede cambiar**: no lo interprete ni lo compare. |
| `trace_id` | Sí | Identificador de la solicitud. Repórtelo a soporte (ver «Cómo reportar un error»). |
| `reason` | No | Motivo de negocio de una lista cerrada (ver más abajo). Estable, como `code`. |
| `details` | Sí | Lista de campos inválidos `{"campo", "motivo"}`; vacía si no aplica. |

Toda respuesta, de éxito o de error, lleva la cabecera `X-Correlation-Id`. Ninguna expone trazas, URLs ni mensajes de los servicios
internos, ni repite datos personales, números de tarjeta o tokens que usted haya enviado.

**Excepción:** `POST /oauth/token` responde con el formato de [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749#section-5.2)
(`{"error": "invalid_client", "error_description": "..."}`), porque es lo que esperan las librerías OAuth2. Su 401 es `invalid_client`
y no usa `TOKEN_EXPIRED`.

## Catálogo de códigos

| HTTP | `code` | Cuándo | Qué hacer |
|---|---|---|---|
| 400 | `INVALID_REQUEST` | Cuerpo, cabecera o parámetro inválido (`details`), o una regla de negocio no se cumple (`reason`). | No reintente; corrija la solicitud. |
| 401 | `INVALID_TOKEN` | Sin token, mal formado o con firma inválida. | No reintente; obtenga un token en `/oauth/token`. |
| 401 | `TOKEN_EXPIRED` | Token vencido. | Pida un token nuevo y repita la solicitud. |
| 403 | `ENDPOINT_NOT_AUTHORIZED` | Su contrato no habilita el endpoint. | No reintente; pida que se habilite. |
| 404 | `RESOURCE_NOT_FOUND` | La cotización, venta, firma o póliza no existe o es de otro socio; o la ruta no existe. | No reintente. |
| 405 | `INVALID_REQUEST` | Método no permitido para la ruta (cabecera `Allow`). | No reintente; use el método correcto. |
| 409 | `IDEMPOTENCY_CONFLICT` | La clave `X-Idempotency-Key` se usó con otro cuerpo. | No reintente con la misma clave. |
| 409 | `IDEMPOTENCY_CONFLICT` + `reason: SOLICITUD_EN_PROCESO` | La solicitud original con esa clave sigue en curso. | **Sí** reintente, con la **misma** clave, tras `Retry-After`. |
| 429 | `RATE_LIMIT_EXCEEDED` | Excedió la cuota por segundo de su contrato. | Espere `Retry-After` segundos y reintente. |
| 500 | `INTERNAL_ERROR` | Fallo no previsto en Socios, o un error interno sin traducción. | Reintente con retroceso exponencial. |
| 503 | `SERVICE_UNAVAILABLE` | Una dependencia (Cotización, Identidad, Suscripción o el almacén) no responde. | Reintente con retroceso exponencial. |
| 503 | `SERVICE_DEGRADED` | El circuito hacia una dependencia está abierto (con `Retry-After`). | Reintente tras `Retry-After`. |

### Valores de `reason`

Con `code` = `INVALID_REQUEST` (salvo `SOLICITUD_EN_PROCESO`, que va con `IDEMPOTENCY_CONFLICT`):

| `reason` | Significado |
|---|---|
| `CLIENTE_NO_REGISTRADO` | El cliente no está registrado en Solventa. |
| `KYC_NO_VERIFICADO` | El cliente no ha verificado su identidad (KYC). |
| `CONSENTIMIENTO_NO_VIGENTE` | El cliente no tiene un consentimiento Open Finance vigente. |
| `COTIZACION_YA_USADA` | La cotización ya se usó en otra venta. |
| `COTIZACION_VENCIDA` | La cotización venció; cotice de nuevo. |
| `COTIZACION_SIN_CUENTA` | La cotización no está asociada a una cuenta que se pueda suscribir. |
| `SIN_METODO_PAGO` | No hay un método de pago válido para cobrar la póliza. |
| `POLIZA_NO_LISTA` | La póliza todavía no está lista para esta operación. |
| `FIRMA_NO_DISPONIBLE` | La firma no está disponible para esta venta (no está en el paso de firma). |
| `FIRMA_VENCIDA` | El plazo para firmar venció. |
| `FIRMA_FALLIDA` | No se pudo verificar la identidad del cliente al firmar; la venta se canceló. |
| `DOCUMENTO_ALTERADO` | El documento que se firma no coincide con el emitido. |
| `OTP_INVALIDO` | El código de firma no es válido. |
| `CERTIFICADO_NO_DISPONIBLE` | El certificado de la póliza no está disponible. |
| `SOLICITUD_EN_PROCESO` | (con `IDEMPOTENCY_CONFLICT`) La solicitud original sigue en curso. |

## Política de reintentos

| Respuesta | ¿Reintentar? | Cómo |
|---|---|---|
| 4xx (salvo los de abajo) | No | Corrija la solicitud, el token o el contrato. |
| 401 `TOKEN_EXPIRED` | Sí, una vez | Pida un token nuevo y repita. |
| 429 | Sí | Espere `Retry-After` segundos. |
| 409 `SOLICITUD_EN_PROCESO` | Sí | Espere `Retry-After` y repita con la **misma** `X-Idempotency-Key`. |
| 503 `SERVICE_DEGRADED` | Sí | Espere `Retry-After`. Si falta la cabecera, use el retroceso exponencial. |
| 500 y 503 `SERVICE_UNAVAILABLE` | Sí | Retroceso exponencial **1, 2, 4, 8 y 16 s**, hasta 5 intentos. |
| Sin respuesta en 10 s | Sí | Reintente con la **misma** `X-Idempotency-Key`: no duplica la operación. |

Reglas generales:

- **Tiempo de espera:** espere hasta 10 s por respuesta. La suma de los tiempos internos de una operación (Identidad 3 s + Suscripción 6 s)
  queda por debajo de ese tiempo.
- **Misma clave al reintentar.** Las escrituras (`POST` de cotizaciones, suscripciones y firma) exigen `X-Idempotency-Key`. Al reintentar
  un 5xx, un 429 o un tiempo agotado, use la **misma** clave: si la operación ya se hizo, recibe la respuesta original
  (`X-Idempotent-Replayed: true`). Una clave nueva crea una operación nueva.
- **Cuándo no reutilizar la clave:** si recibe 409 `IDEMPOTENCY_CONFLICT` sin `reason`, esa clave ya se usó con otro cuerpo.
- **Servicio degradado:** una cotización puede volver con `degradado: true` (perfil por defecto) y sigue siendo 201. Un 503
  `SERVICE_DEGRADED` de Cotización avisa que la cotización puede volver con un perfil por defecto cuando el circuito cierre.

## Idioma

Los mensajes son en español. `code` y `reason` son estables y no se localizan; `message` puede cambiar. La API no localiza según
`Accept-Language`.

## Cómo reportar un error

Envíe a soporte el `trace_id` del cuerpo y el `X-Correlation-Id` de la respuesta, la ruta, la hora (UTC) y el `code`. Con el `trace_id`
se llega al log de Socios y, desde allí, al del servicio interno que falló. No envíe documentos de clientes, números de tarjeta,
tokens ni secretos.

## Qué cambió respecto a HU32 y HU33

- La validación pasa de 422 a **400** `INVALID_REQUEST`.
- Los conflictos de negocio (cotización usada o vencida, firma vencida, OTP...) pasan de 409/422 a **400** con `reason`.
  El 409 `IDEMPOTENCY_CONFLICT` queda solo para la clave de idempotencia.
- El motivo de negocio pasa de `details` a `error.reason`; `details` solo lleva campos inválidos.
- Rutas inexistentes y métodos no permitidos responden en este formato (antes, `{"detail": "Not Found"}`).
- El 500 conserva el `trace_id` y la cabecera `X-Correlation-Id`.
- Nuevo 503 `SERVICE_DEGRADED` con `Retry-After` (circuito abierto).
- El tiempo máximo hacia Suscripción baja de 10 a 6 s.
