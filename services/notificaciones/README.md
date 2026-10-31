# MS Notificaciones

Confirmación al cliente cuando su póliza queda emitida (HU22): un **correo** con los detalles y el enlace de descarga del
certificado, y una **notificación push** de alta prioridad. Decisión completa en el
[ADR-0020](../../docs/adr/0020-ms-notificaciones-correo-ses-y-push-simulado.md). Fuera de alcance: preferencias de
notificación, recordatorios de vencimiento y avisos de cobro y siniestro.

## Cómo fluye

```
MS Suscripción ──poliza_emitida──▶ solventa.polizas.ciclovida ──▶ consumidor (solventa-notificaciones.polizas)
                                                                   │ 1. un envío PENDIENTE por canal
                                                                   ▼    (evento de origen + canal: único)
                                          procesador ──▶ CORREO: Identidad (correo) + Suscripción (enlace) ──▶ Amazon SES
                                                    └──▶ PUSH:   bandeja del cliente (adaptador simulado)
                                                           │ 2. anota el intento y entrega; un fallo se reintenta con espera creciente
                                                           ▼
App móvil ──GET /v1/notificaciones──▶ la bandeja ──▶ notificación local de Android (prioridad alta)
```

Un fallo de envío **nunca toca la póliza**: ya está emitida. Agotados los intentos, el envío queda `FALLIDO` y el
operador lo ve en `GET /v1/notificaciones/envios-fallidos`.

## Endpoints

Contrato en [`docs/api/openapi.json`](docs/api/openapi.json); colección en `postman/` (carpeta «MS Notificaciones (HU22)»).
Publicados en el borde: `/v1/dispositivos` y `/v1/notificaciones`.

| Método y ruta | Quién | Qué hace |
|---|---|---|
| `POST /v1/dispositivos` | cliente | Registra el dispositivo (lo genera la app) y su idioma. Idempotente. |
| `GET /v1/notificaciones` | cliente | La bandeja del push: por defecto solo las nuevas (`solo_nuevas=true`), es decir, las no confirmadas y creadas desde que el cliente registró su primer dispositivo. |
| `POST /v1/notificaciones/{id}/recibida` | cliente | La app confirma que ya la mostró. Una ajena responde 404. |
| `GET /v1/notificaciones/envios-fallidos` | operador (JWT) | Envíos que agotaron sus reintentos: canal, intentos y nombre del error, sin correo. |

El cliente se identifica con su token; Identidad lo valida. El operador, con el JWT HS256 que emite Identidad (ADR-0008).

## Canales (ADR-0004)

| Canal | Adaptador | Qué hace |
|---|---|---|
| Push | `PushSimulado` | Deja el mensaje en `bandeja_push`. Se simula el transporte; la idempotencia, los reintentos y los textos son reales. |
| Correo | `CorreoSimulado` | No envía; solo cuenta y deja una huella en el log. |
| Correo | `CorreoSes` | Amazon SES (`CORREO_PROVEEDOR=ses`). En sandbox solo envía a direcciones verificadas. |

## Variables de entorno

| Variable | Para qué |
|---|---|
| `NOTIFICACIONES_DATABASE_URL` / `DATABASE_URL` | PostgreSQL (esquema `notificaciones`). Sin ella solo arranca en modo desarrollo, con SQLite. |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD` | La conexión a MSK (plantilla HS2). |
| `IDENTIDAD_URL`, `IDENTIDAD_SERVICE_KEY` | Sesión del cliente y su contacto. |
| `SUSCRIPCION_URL`, `SUSCRIPCION_SERVICE_KEY` | Enlace firmado de descarga del certificado. |
| `URL_PUBLICA` | Base de los enlaces del correo (el borde). Sin ella el correo sale sin enlace. |
| `CORREO_PROVEEDOR` (`simulado` \| `ses`), `CORREO_REMITENTE`, `AWS_REGION` | El canal de correo. |
| `NOTIFICACIONES_MAX_INTENTOS`, `…_REINTENTO_BASE_S`, `…_REINTENTO_MAXIMO_S` | Reintentos (5, 5 s y 300 s por defecto). |
| `ASESOR_JWT_SECRETO`, `ASESOR_JWT_EMISOR` | Validación del JWT del operador. |

## Privacidad

El correo del cliente no se guarda: se pide a Identidad al enviar. Los logs lo enmascaran (`FiltroDatosPersonales`) y los
errores guardan solo el nombre de la clase o el código del proveedor. La versión del push para la pantalla de bloqueo no
lleva datos de la póliza.
