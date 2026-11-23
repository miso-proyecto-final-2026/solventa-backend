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

- **Idempotencia.** Tres escrituras exigen `X-Idempotency-Key`: `POST /partners/v1/cotizaciones`,
  `POST /partners/v1/suscripciones` y `POST .../firma`. Es un valor único por operación que usted genera (por ejemplo, un
  UUID), de 1 a 128 caracteres (letras, dígitos y `. _ : -`). Sin ella, la respuesta es 400 `INVALID_REQUEST`.
  - Si una respuesta no le llega, repita la solicitud **con la misma clave y el mismo cuerpo**: recibirá la respuesta original
    (`X-Idempotent-Replayed: true`) sin que la operación se repita.
  - Usar la clave con otro cuerpo es un error (409 `IDEMPOTENCY_CONFLICT`).
  - `POST .../firma/codigo` **no** lleva clave ni cuerpo.
- **Cuota.** Su contrato fija cuántas solicitudes por segundo puede hacer. Si la supera, recibe 429 con `Retry-After`.
- **Errores.** Todo error de `/partners/v1` tiene la misma forma, con un `code` de un catálogo cerrado y un `trace_id` para
  reportarlo. Qué significa cada código y cuándo reintentar está en `errores-y-reintentos.md`.
- **Datos personales.** `referencia_cliente` es el identificador del cliente **en su sistema**: no envíe ahí documentos ni
  nombres. El documento del cliente solo va en el campo `cliente`, en el cuerpo, nunca en la URL.

## 4. El recorrido de una venta

El contrato describe cada campo; este es el orden de las llamadas.

1. **Cotizar** — `POST /partners/v1/cotizaciones`, con un `producto` y sus `datos` (ver «Productos»).
   - **Con `cliente`** (tipo y número de documento de un cliente registrado en Solventa), la cotización se puede vender.
   - **Sin `cliente`**, es solo informativa: sale con `aproximada: true` y un `aviso` para mostrar, porque no usó el perfil
     del cliente y el precio puede cambiar al comprar.
   - `degradado: true` significa que un proveedor de datos no respondió y el precio usó un perfil por defecto. Sigue siendo
     una cotización válida (201).
2. **Vender** — `POST /partners/v1/suscripciones`, con la cotización, el mismo `cliente`, el token de pago (`metodo_pago`) y
   `acepta_terminos`. Responde 202 con el `id` de la venta: la decisión y el cobro siguen en segundo plano.
3. **Consultar** — `GET /partners/v1/polizas/{id}` con ese `id`. Solventa no le avisa: consulte cada 2 a 5 segundos, sin
   pasar su cuota, hasta que la venta esté **lista para firmar**, es decir, cuando se cumple todo esto:
   - `estado` es `COBRO_REALIZADO`;
   - `paso_actual` es `FIRMA`;
   - `firma.estado` es `PENDIENTE`;
   - `poliza.documento_estado` es `GENERADO`.

   Si la venta queda en revisión manual (`decision.decision` = `DERIVADA`) o termina, vea «Estados de una venta».
4. **Firmar dentro de su app:**
   1. muestre al cliente el documento (`GET .../documento-firma` y `.../documento-firma/pdf`);
   2. pida que Solventa le envíe el código de un solo uso (`POST .../firma/codigo`, sin cuerpo ni clave, responde 202);
   3. envíe la firma con el código que el cliente le da (`POST .../firma`).

   La firma vence 24 horas después del cobro (`firma.vence_en`).
5. **Consultar la póliza emitida** — `GET /partners/v1/polizas/{id}` hasta que `estado` sea `EMITIDA` (`poliza.estado`
   `VIGENTE`). El cliente recibe su certificado directamente de Solventa.

### Productos

| `producto` | Campos de `datos` (todos obligatorios) |
|---|---|
| `microseguro_vida` | `edad`: entero, de 18 a 65. `suma_asegurada`: entero en COP, de 5.000.000 a 100.000.000. |
| `proteccion_dispositivos` | `tipo_dispositivo`: `celular`, `portatil` o `tableta`. `valor_dispositivo`: entero en COP, de 300.000 a 15.000.000. `antiguedad_meses`: entero, de 0 a 36. |

Un producto desconocido, un campo que falta o un valor fuera de rango responden 400 `INVALID_REQUEST` con `details`.

### Condiciones del cliente

Cuando se envía `cliente`, Solventa revisa sus condiciones **al cotizar y otra vez al vender**, en este orden. La primera que
falla responde 400 `INVALID_REQUEST` con su `reason`:

1. el cliente está registrado (`CLIENTE_NO_REGISTRADO`);
2. verificó su identidad (`KYC_NO_VERIFICADO`);
3. tiene un consentimiento de datos vigente (`CONSENTIMIENTO_NO_VIGENTE`).

Por eso un cliente sin consentimiento se rechaza ya al cotizar y nunca llega a tener una cotización vendible.

## 5. Estados de una venta

`GET /partners/v1/polizas/{id}` devuelve `estado`, `paso_actual`, y `decision`, `cobro`, `firma` y `poliza` a medida que
existen.

| Situación | `estado` / `paso_actual` | Qué hacer |
|---|---|---|
| Decidiendo | `EN_PROGRESO` / `DECISION`, sin `decision` | Siga consultando. |
| **En revisión manual** | `EN_PROGRESO` / `DECISION`, `decision.decision` = `DERIVADA` | Siga consultando; vea abajo. |
| Aprobada, cobrando | `APROBADA` / `COBRO` | Siga consultando. |
| Cobro rechazado | `APROBADA` / `COBRO`, `cobro.estado` = `RECHAZADO` | La venta no avanza a la firma. Informe el `cobro.motivo` al cliente. Para intentar con otro medio de pago, cotice de nuevo y haga una venta nueva: la cotización ya quedó usada. |
| Lista para firmar | `COBRO_REALIZADO` / `FIRMA`, `firma.estado` = `PENDIENTE` y `poliza.documento_estado` = `GENERADO` | Haga la firma (paso 4). |
| Firmada | `FIRMADA` / `EMISION` | Siga consultando hasta `EMITIDA`. |
| **Emitida** (terminal) | `EMITIDA` | La póliza está vigente. |
| **Rechazada** (terminal) | `RECHAZADA` / `DECISION` | La venta termina; los motivos están en `decision.motivos`. |
| **Compensada** (terminal) | `COMPENSADA`, `firma.estado` = `VENCIDA` o `FALLIDA` | La firma venció o falló; Solventa devolvió el cobro y anuló la póliza. |

**Valores de `decision.decision`:**

- **`APROBADA`:** la venta sigue sola al cobro.
- **`RECHAZADA`:** la venta termina.
- **`DERIVADA`:** Solventa no puede decidir sola (por ejemplo, porque el perfil financiero del cliente está incompleto) y la
  venta **pasa a revisión manual de un asesor de Solventa**.
  - No es un error ni hay que enviar nada más: la venta queda en `DECISION` hasta que el asesor la resuelva.
  - Siga consultando con un intervalo más largo (por ejemplo, cada 30 a 60 segundos) y muéstrele al cliente que su solicitud
    está en revisión.
  - No hay un plazo automático. Si necesita apurarla, escriba a soporte con el `id` de la venta.
  - Si el asesor aprueba, `decision.decision` cambia a `APROBADA` y la venta sigue al cobro. Si la ajusta, sigue al cobro
    con el nuevo `monto_asegurado`. Si la rechaza, `estado` pasa a `RECHAZADA`.
- **`AJUSTADA`** (decisión automática): Solventa aprobaría un monto asegurado menor y espera que el cliente lo acepte. La v1
  de la API no tiene cómo aceptarlo, así que esa venta no avanza: cotice de nuevo con un monto menor.

**`decision.motivos`** explica la decisión automática. Si un asesor resolvió una venta derivada, conserva los motivos de la
derivación aunque `decision.decision` ya no sea `DERIVADA`: muestre la decisión, no los motivos.

## 6. Datos del ambiente de pruebas

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

- **`CC 1000000004`** se rechaza ya al cotizar con `cliente`: 400 `CONSENTIMIENTO_NO_VIGENTE`.
- **En staging, una venta a `CC 1000000001` puede quedar en revisión manual** (`DERIVADA`) por los datos de prueba. Es la
  oportunidad de probar ese estado. Para que avance, escriba a soporte con el `id` de la venta y un asesor la resolverá.

## 7. Antes de pasar a producción

Compruebe en staging que su integración:

- obtiene y renueva el token;
- cotiza, vende, firma y consulta la póliza emitida;
- trata una venta en revisión manual (`DERIVADA`) como un estado de espera, y reconoce los estados terminales;
- reintenta con la misma `X-Idempotency-Key` cuando no recibe respuesta;
- respeta `Retry-After` en los 429 y en los 503;
- muestra al usuario un mensaje comprensible para cada `code` y guarda el `trace_id` de los errores.
