# MS Pagos

Cobro de la prima (HU18) con una pasarela de pago detrás de un puerto del dominio. Registra transacciones e intentos,
cobra una sola vez por `idempotency_key` y concilia cuando la respuesta de la pasarela no llega. Decisión completa en el
[ADR-0014](../../docs/adr/0014-ms-pagos-cobro-idempotente-y-conciliacion.md). También expone a la consola web la
gestión de cobros fallidos (HU19, enmienda del ADR-0014). Fuera de alcance: indemnizaciones (HU29/HU30).

## Cómo fluye un cobro

```
MS Suscripción ──cobro_prima_solicitado──▶ solventa.suscripcion.comandos ──▶ consumidor (solventa-pagos.cobros)
                                                                               │ 1. registra la transacción PENDIENTE
                                                                               ▼    (idempotency_key única)
                                                         procesador ──▶ pasarela (adaptador)
                                                           │ 2. anota el intento y cobra con la misma clave
                                                           │ 3. sin respuesta: consulta la clave antes de reintentar
                                                           ▼
MS Suscripción ◀──cobro_prima_confirmado / rechazado── solventa.pagos.resultados ◀── cola de salida (outbox)
```

`POST /v1/pagos/cobros` es una entrada interna al mismo caso de uso (pruebas y operación); la saga usa el evento.

## Endpoints internos (sin borde público)

Todos exigen `X-Service-Key` con una clave de `PAGOS_API_KEYS` (401 sin cabecera, 403 con una clave desconocida).
Contrato en [`docs/api/openapi.json`](docs/api/openapi.json); colección en `postman/` (carpeta «MS Pagos (HU18)»).

| Método y ruta | Qué hace |
|---|---|
| `POST /v1/pagos/tokenizar` | Registra un **token de la pasarela** con su marca y últimos 4 (`tokens_tarjeta`). Rechaza con 422 `DATO_DE_TARJETA_NO_PERMITIDO` cualquier texto con forma de número de tarjeta (SAQ-A: el PAN solo lo ve la pasarela). |
| `POST /v1/pagos/cobros` | Cobra la prima. 201 con el resultado, 202 si queda `PENDIENTE` (sin respuesta de la pasarela), 200 + `Idempotent-Replay: true` al repetir la clave, 409 si la clave ya se usó con otro monto o suscripción. |
| `GET /v1/pagos/transacciones/{id}` | Estado de la transacción: `PENDIENTE`, `APROBADA` o `RECHAZADA` (con código, motivo y `gracia_hasta`). Nunca devuelve el token. |

## Cobros fallidos (HU19, publicado en el borde)

Único prefijo de Pagos publicado en el borde. Exige `Authorization: Bearer <JWT>` emitido por Identidad con rol
`ASESOR` u `OPERADOR` (401 `NO_AUTENTICADO` sin token o con uno vencido, 403 `SIN_PERMISO` con otro rol). La clave de
servicio no abre estas rutas. Colección: carpeta «Cobros fallidos (HU19)».

| Método y ruta | Qué hace |
|---|---|
| `GET /v1/pagos/cobros-fallidos` | Bandeja paginada (`pagina`, `tamano` 1–100, `estado` repetible, `por_vencer`), ordenada por `gracia_hasta`. Cada caso: monto, intentos, último intento, motivo, `gracia_hasta`, `estado_gracia` (`VIGENTE`, `POR_VENCER` si quedan menos de `PAGOS_HORAS_POR_VENCER` horas, `VENCIDA`) y la tarjeta (marca y últimos 4). |
| `GET /v1/pagos/cobros-fallidos/{id}` | Detalle: cada cobro (original y reintentos) con sus intentos, códigos de la pasarela y notas. 404 `CASO_NO_ENCONTRADO`. |
| `POST /v1/pagos/cobros-fallidos/{id}/notas` | Nota de seguimiento (1–1000 caracteres) con el autor del JWT. 422 `DATO_DE_TARJETA_NO_PERMITIDO` si trae algo con forma de número de tarjeta. |
| `POST /v1/pagos/cobros-fallidos/{id}/reintentar` | Cobra otra vez con el token ya registrado y la clave `cobro_prima:<suscripcion_id>:<n>`. 200 con `APROBADO`/`RECHAZADO`, 202 con `EN_PROCESO` (`detalle_pasarela` = `NO_DISPONIBLE` o `SIN_RESPUESTA`), 409 `CASO_YA_RECUPERADO`. La misma `Idempotency-Key` (o un reintento aún `PENDIENTE`) devuelve el mismo reintento con `Idempotent-Replay: true`. |

Un cobro de prima rechazado abre el caso (`PENDIENTE`). Cuando termina la gracia sin recuperarlo, un detector (cada
`PAGOS_DETECTOR_GRACIA_S` segundos) lo pasa a `VENCIDO` y publica `cobro_gracia_vencida` en `solventa.notificaciones`
para el operador. Un reintento aprobado lo deja `RECUPERADO` y el resultado sigue llegando a la saga por
`solventa.pagos.resultados`. La bandeja y el detalle nunca devuelven el token ni datos de contacto del cliente.

## Pasarela simulada

Es el único adaptador hoy (`PASARELA_PROVEEDOR=simulador`). Se comporta como una pasarela real en lo que importa: es
idempotente por `idempotency_key` (repetir la orden devuelve el primer resultado sin cobrar otra vez) y permite consultar
un cobro por su clave. Su libro (`pagos.simulador_cargos`) vive en la base para que cualquier réplica pueda conciliar; con
una pasarela real esa tabla no se usa.

### Modos

El modo por defecto sale de `PASARELA_SIM_MODO`. Para probar en staging sin redesplegar, el **prefijo del token** lo
cambia por cobro.

| Modo | Prefijo del token | Qué pasa | Qué hace MS Pagos | Caso |
|---|---|---|---|---|
| `APROBAR` (por defecto) | `tok_sim_aprobar_…` | Cobra y responde aprobado. | `APROBADA` con la referencia; publica `cobro_prima_confirmado`. | Criterio 1 |
| `RECHAZAR` | `tok_sim_rechazar_…` | No cobra; responde `FONDOS_INSUFICIENTES`. | `RECHAZADA` con código y motivo, abre el período de gracia (`gracia_hasta`); publica `cobro_prima_rechazado`. | Criterio 2 |
| `TARDAR` | `tok_sim_tardar_…` | Cobra, pero responde tras `PASARELA_SIM_DEMORA_S` (10 s), más que `PASARELA_TIMEOUT_S` (5 s). | El intento queda `SIN_RESPUESTA`; el siguiente consulta la clave, encuentra el cobro y lo marca `CONCILIADO`. | Criterio 4 |
| `PERDER_RESPUESTA` | `tok_sim_perder_…` | Cobra y la respuesta se pierde. | Igual que `TARDAR`: concilia sin volver a cobrar (un solo cargo en el libro). | Criterio 4, EC-D1.2 |

El token de la app móvil (`tok_sim_<aleatorio>`, HU17) usa el modo por defecto. Un token que no empieza por `tok_` se
rechaza con `TOKEN_INVALIDO`. Marca y últimos 4 se derivan del token (el simulador no ve ninguna tarjeta).

### Fallas de la pasarela y cómo se tratan

| Falla | Traducción al dominio | Reintento |
|---|---|---|
| Tiempo de espera agotado / respuesta perdida | `PasarelaSinRespuesta`: pudo cobrar | Consulta la clave; si no hay cobro, lo pide de nuevo **con la misma clave** |
| Conexión rechazada | `PasarelaNoDisponible`: no recibió la orden | Reintenta con la misma clave |
| Error desconocido | Se trata como «sin respuesta» | Igual que el primero |

La espera entre intentos empieza en `PAGOS_REINTENTO_BASE_S` (5 s) y se duplica hasta `PAGOS_REINTENTO_MAXIMO_S` (300 s).
Una transacción nunca se crea dos veces ni se da por perdida: queda `PENDIENTE` hasta tener una respuesta cierta.

## Datos de tarjeta

- El PAN nunca entra: la API y el consumidor rechazan cualquier texto con forma de número de tarjeta.
- El token se guarda solo en `tokens_tarjeta.token`; los eventos, la API y los logs nunca lo llevan.
- Logs en JSON con `trace_id` y `correlation_id` (`app/logs.py`). Un filtro enmascara además cualquier PAN o token que
  llegue a un log, venga de donde venga.
- `tests/test_seguridad.py` recorre los flujos, los logs y todas las tablas buscando números que pasen Luhn y el token.
- Limitación: un comando que agota sus reintentos va a `solventa.suscripcion.comandos.dlq` tal como llegó (con el token,
  como en el topic de origen). La cola de fallidos tiene los mismos permisos y retención que el topic.

## Trabajar en local

```bash
cd services/pagos
pip install -r requirements.txt -r ../../requirements-dev.txt
pytest                                    # las pruebas con contenedores se omiten sin Docker
SOLVENTA_MODO_DESARROLLO=1 PAGOS_API_KEYS=postman=dev-pagos-key \n  ASESOR_JWT_SECRETO=dev-secreto-asesor-de-al-menos-32-bytes uvicorn app.main:app --port 8005
```

Variables: ver la tabla del [README del monorepo](../../README.md).
