# ADR-0020 — MS Notificaciones: correo con Amazon SES y push simulado

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-30
- **Historia:** HU22 (Sprint 2, F10). Requisito RQ-L2. Cierra el flujo de la compra: HU20 (póliza), HU21 (firma) y HU22 (confirmación)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin código compartido entre servicios), [ADR-0004](0004-proveedores-externos-detras-de-interfaces.md) (proveedores detrás de una interfaz), [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (plantilla de eventos), [ADR-0014](0014-ms-pagos-cobro-idempotente-y-conciliacion.md) (registrar, entregar aparte, reintentar) y [ADR-0017](0017-ms-suscripcion-documento-de-poliza.md) (documento de la póliza)

## Contexto

Al emitirse la póliza, Suscripción publica `poliza_emitida` en `solventa.polizas.ciclovida` y nadie lo consume. El cliente
debe recibir un certificado descargable, un correo con los detalles y un push de alta prioridad, sin duplicados aunque el
evento se reentregue, sin que un fallo del envío toque la póliza, y sin datos sensibles en la pantalla de bloqueo. No
existe un servicio de notificaciones, Identidad no guarda el idioma ni un identificador de dispositivo, y el proyecto
excluye las integraciones reales (T4).

## Decisión

1. **Servicio nuevo `notificaciones`**, con esquema propio y sin claves foráneas hacia otros servicios. Consume
   `poliza_emitida` con el grupo `solventa-notificaciones.polizas` (el grupo empieza por el nombre del servicio, que es lo
   que permite su política de IAM). Reentregar el evento no envía dos veces: la clave es el `event_id` de origen más el
   canal, `UNIQUE` en la base, además del registro de eventos procesados de la plantilla.
2. **Dos momentos, como Pagos (ADR-0014).** *Registrar*, en la transacción del consumidor, una fila `envios` por canal
   (`PUSH` y `CORREO`); no llama a ningún proveedor, porque un correo ya enviado no se deshace si la transacción se
   revierte. *Entregar*, en un proceso de fondo que toma los pendientes con `FOR UPDATE SKIP LOCKED`, anota el intento y
   **reserva** la fila antes de llamar al canal; si el proceso cae, la reserva vence y se reintenta.
3. **Los fallos no revierten nada (criterio 6).** Un fallo se reintenta con espera creciente (5 s, 10 s, 20 s… con tope de
   300 s, `NOTIFICACIONES_MAX_INTENTOS` = 5). Agotados los intentos el envío queda `FALLIDO`, y el operador lo ve en
   `GET /v1/notificaciones/envios-fallidos` (JWT con rol `OPERADOR`, ADR-0008). Cada canal falla por separado: que el correo
   falle no impide que el push se entregue.
4. **Correo con Amazon SES, detrás de `CanalCorreo`.** SNS no sirve para correo transaccional a destinatarios arbitrarios
   (envía a suscriptores que confirman). La cuenta de SES está en *sandbox*: solo envía a direcciones verificadas, que basta
   para staging. El adaptador simulado se conserva para pruebas y desarrollo y se elige con `CORREO_PROVEEDOR`.
5. **Push simulado, detrás de `CanalPush`.** Entregar es dejar el mensaje en `bandeja_push`; la app la consulta
   (`GET /v1/notificaciones`) y lanza una notificación local de importancia alta, y confirma con
   `POST /v1/notificaciones/{id}/recibida`. Se simula solo el transporte: la idempotencia, los reintentos, las plantillas y
   la bandeja son reales. Un push real con SNS y FCM sería otro adaptador (token de FCM en lugar del identificador del
   dispositivo, servicio de mensajes de Firebase en la app en lugar del sondeo) y queda fuera de esta historia.
6. **Idioma.** El de los textos es el del último dispositivo que el cliente registró (`POST /v1/dispositivos`, con el idioma
   del teléfono), con es-CO por defecto. Se fija al registrar el envío, así que los reintentos no cambian de idioma.
7. **Contenido seguro (criterio 8).** El push lleva una versión pública para la pantalla de bloqueo («Solventa · Tienes una
   notificación nueva»), sin número de póliza ni importes; el detalle solo se ve dentro de la app. El correo sí lleva el
   número y las fechas de la póliza.
8. **Privacidad.** El correo del cliente **no se guarda**: se pide a Identidad al enviar, mediante un endpoint interno
   aparte, `GET /v1/contactos-cliente/{id}` (nombre y correo, sin documento ni celular). El resumen de HU20 se diseñó para
   no exponer el correo y no se ensancha. Los logs enmascaran correos y números con forma de tarjeta, y los errores guardan
   solo el nombre de la clase o el código del proveedor, porque el mensaje de SES puede traer la dirección.
9. **Certificado y enlace del correo (en Suscripción).** El certificado es un PDF que repite los datos de la póliza emitida,
   en es-CO y en-US, guardado en `certificados/` del bucket. El titular lo descarga con su sesión; para el correo,
   Notificaciones pide a Suscripción un enlace: un token firmado con HMAC-SHA256 que lleva póliza, idioma y vencimiento
   (24 h, provisional), que se descarga sin sesión (vencido → 410, alterado → 401). No se usa un enlace prefirmado de S3
   porque caduca con las credenciales temporales del rol. Quien tenga el enlace puede descargar el PDF hasta que venza;
   la alternativa, exigir sesión, no sirve para un enlace en un correo. Si Suscripción no tiene la clave de los enlaces, el
   correo sale sin enlace y remite a la aplicación; si Suscripción no responde, el envío se reintenta.

## Consecuencias

- **Infra:** el servicio nuevo necesita su entrada en `services.yaml` (base, secretos, consumo de
  `solventa.polizas.ciclovida`, rutas del borde `/v1/dispositivos`, `/v1/notificaciones` y `/v1/certificados`), su rol de
  despliegue en CD, el permiso `ses:SendEmail` limitado a la identidad verificada y el permiso de Suscripción sobre
  `certificados/`. Para el correo real, una dirección verificada en SES (confirmación manual).
- **Límites del push simulado:** sin Firebase no hay push con la app cerrada; la notificación aparece cuando la app está
  abierta o se abre.
- **Un solo correo por póliza y evento.** Si Identidad no conoce al cliente, el envío agota sus intentos y queda `FALLIDO`:
  no se inventa un destinatario.
- Alternativas descartadas: notificar dentro de Suscripción (mezcla la saga con un proveedor externo y la carga del envío);
  SNS para el correo; push real con SNS y FCM (Firebase, credenciales nuevas y cambios en la app, sin aportar a los criterios);
  un enlace prefirmado de S3 (caduca con las credenciales del rol).
