# MS Pagos

Cobro de la prima (HU18) con una pasarela de pago detrás de un puerto del dominio. Registra transacciones e intentos,
cobra una sola vez por `idempotency_key` y concilia cuando la respuesta de la pasarela no llega. Decisión completa en el
[ADR-0014](../../docs/adr/0014-ms-pagos-cobro-idempotente-y-conciliacion.md). Fuera de alcance: indemnizaciones
(HU29/HU30) y gestión humana de cobros fallidos (HU19).

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

## Endpoints (internos, sin borde público)

Todos exigen `X-Service-Key` con una clave de `PAGOS_API_KEYS` (401 sin cabecera, 403 con una clave desconocida).
Contrato en [`docs/api/openapi.json`](docs/api/openapi.json); colección en `postman/` (carpeta «MS Pagos (HU18)»).

| Método y ruta | Qué hace |
|---|---|
| `POST /v1/pagos/tokenizar` | Registra un **token de la pasarela** con su marca y últimos 4 (`tokens_tarjeta`). Rechaza con 422 `DATO_DE_TARJETA_NO_PERMITIDO` cualquier texto con forma de número de tarjeta (SAQ-A: el PAN solo lo ve la pasarela). |
| `POST /v1/pagos/cobros` | Cobra la prima. 201 con el resultado, 202 si queda `PENDIENTE` (sin respuesta de la pasarela), 200 + `Idempotent-Replay: true` al repetir la clave, 409 si la clave ya se usó con otro monto o suscripción. |
| `GET /v1/pagos/transacciones/{id}` | Estado de la transacción: `PENDIENTE`, `APROBADA` o `RECHAZADA` (con código, motivo y `gracia_hasta`). Nunca devuelve el token. |

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
SOLVENTA_MODO_DESARROLLO=1 PAGOS_API_KEYS=postman=dev-pagos-key uvicorn app.main:app --port 8005
```

Variables: ver la tabla del [README del monorepo](../../README.md).
