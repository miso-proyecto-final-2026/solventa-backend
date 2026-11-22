# API de socios de Solventa — guía de inicio

Esta guía es para el equipo técnico de un socio de distribución (un banco, una fintech, un comercio) que quiere ofrecer los
seguros de Solventa dentro de su propia app. Con ella, el contrato OpenAPI (`openapi.json`) y la referencia de errores
(`errores-y-reintentos.md`) se puede integrar sin hablar con el equipo de Solventa. Si algo no queda claro, repórtelo:
es un defecto de esta documentación.

## 1. Lo que recibe de Solventa

| Qué | Para qué |
|---|---|
| **Contrato API** | Los endpoints que su contrato habilita y su cuota de solicitudes por segundo. |
| **`client_id` y `client_secret`** | Las credenciales OAuth2 (client credentials). El secreto se entrega por un canal aparte; guárdelo como un secreto, nunca en el código ni en un repositorio. |
| **URL del ambiente de pruebas** | El ambiente de staging, con datos sintéticos y sin cobros reales. Las credenciales de producción se entregan después de aprobar las pruebas en staging. |

Todos los endpoints cuelgan de esa URL base: `/oauth/token` y `/partners/v1/...`. La versión va en la ruta (`v1`); una versión
nueva se publica al lado, sin cambiar la `v1`.

## 2. Autenticarse

1. Pida un token con sus credenciales, en el cuerpo como formulario (`application/x-www-form-urlencoded`) o por HTTP Basic:

   ```http
   POST /oauth/token
   Content-Type: application/x-www-form-urlencoded

   grant_type=client_credentials&client_id=<su-client-id>&client_secret=<su-secreto>
   ```

   La respuesta trae `access_token`, `token_type` (`Bearer`) y `expires_in` (segundos). Los errores de este endpoint siguen
   RFC 6749 (`{"error": "invalid_client", ...}`); es el único que no usa el formato del catálogo.
2. Envíe el token en cada llamada a `/partners/v1`: `Authorization: Bearer <access_token>`.
3. Reutilice el token hasta que venza. Si recibe 401 `TOKEN_EXPIRED`, pida otro.

`GET /partners/v1/status` es público: sirve para comprobar la conexión antes de tener credenciales.

## 3. Reglas que aplican a todas las llamadas

- **Idempotencia.** Las escrituras (`POST` de cotizaciones, suscripciones y firma) exigen `X-Idempotency-Key`, un valor único
  por operación que usted genera (por ejemplo, un UUID). Si una respuesta no le llega, repita la solicitud **con la misma
  clave y el mismo cuerpo**: recibirá la respuesta original sin que la operación se repita. Usar la clave con otro cuerpo es
  un error (409).
- **Cuota.** Su contrato fija cuántas solicitudes por segundo puede hacer. Si la supera, recibe 429 con `Retry-After`.
- **Errores.** Todo error de `/partners/v1` tiene la misma forma, con un `code` de un catálogo cerrado y un `trace_id` para
  reportarlo. Qué significa cada código y cuándo reintentar está en `errores-y-reintentos.md`.
- **Datos personales.** `referencia_cliente` es el identificador del cliente **en su sistema**: no envíe ahí documentos ni
  nombres. El documento del cliente solo va en el campo `cliente`, en el cuerpo, nunca en la URL.

## 4. El recorrido de una venta

El contrato describe cada campo; este es el orden de las llamadas.

1. **Cotizar** — `POST /partners/v1/cotizaciones`. Con `cliente` (tipo y número de documento de un cliente registrado en
   Solventa), la cotización se puede vender; sin él, es solo informativa.
2. **Vender** — `POST /partners/v1/suscripciones`, con la cotización, el mismo `cliente`, el token de pago (`metodo_pago`) y
   `acepta_terminos`. Responde 202 con el `id` de la venta: el proceso sigue en segundo plano (decisión y cobro).
3. **Consultar** — `GET /partners/v1/polizas/{id}` con ese `id`. Solventa no le avisa: consulte hasta que la venta esté lista
   para firmar.
4. **Firmar dentro de su app** — muestre al cliente el documento (`GET .../documento-firma` y `.../documento-firma/pdf`), pida
   que Solventa le envíe el código de un solo uso (`POST .../firma/codigo`) y envíe la firma con el código que el cliente le
   da (`POST .../firma`).
5. **Consultar la póliza emitida** — `GET /partners/v1/polizas/{id}` hasta que el estado sea `EMITIDA`. El cliente recibe su
   certificado directamente de Solventa.

## 5. Datos del ambiente de pruebas

Staging no tiene clientes reales. Use estos datos sintéticos:

| Dato | Valor |
|---|---|
| Cliente registrado, con KYC verificado y consentimiento vigente | Cédula (`CC`) `1000000001` |
| Cliente cuyo consentimiento fue revocado | `CC` `1000000004` |
| Código de firma (OTP) | `123456`: en staging es simulado y fijo |
| Token de pago aprobado | Cualquier valor con el formato `tok_sim_<identificador>`, por ejemplo `tok_sim_0f8fad5b-d9cb-469f-a165-70867728950e` |
| Token de pago rechazado | `tok_sim_rechazar_<identificador>` |

Nunca envíe un número de tarjeta en `metodo_pago`: se rechaza. En producción, ese valor es el token que le entrega la pasarela
de pagos al tokenizar la tarjeta en su app.

## 6. Antes de pasar a producción

Compruebe en staging que su integración:

- obtiene y renueva el token;
- cotiza, vende, firma y consulta la póliza emitida;
- reintenta con la misma `X-Idempotency-Key` cuando no recibe respuesta;
- respeta `Retry-After` en los 429 y en los 503;
- muestra al usuario un mensaje comprensible para cada `code` y guarda el `trace_id` de los errores.
