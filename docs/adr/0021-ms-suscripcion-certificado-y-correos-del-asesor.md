# ADR-0021 — MS Suscripción: certificado y correos pedidos por el asesor

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-11-01
- **Historia:** HU39 (Sprint 2, F10). Requisito RQ-I2
- **Depende de:** [ADR-0020 de venta asistida](0020-ms-suscripcion-venta-asistida.md) (rutas propias, mismos servicios), [ADR-0020 de notificaciones](0020-ms-notificaciones-correo-ses-y-push-simulado.md) (certificado y correo) y [ADR-0019](0019-ms-suscripcion-firma-electronica-y-compensacion.md) (firma)

## Contexto

Cuando se escribió el ADR de venta asistida, HU22 no existía y se asumió «certificado = copia firmada». HU22 creó un certificado de cobertura distinto, que solo descarga el titular con su sesión, y un correo de confirmación que sale al consumir `poliza_emitida`. HU39 pide además que el asesor descargue el certificado y lo reenvíe al cliente.

## Decisión

1. **El certificado y la copia firmada son documentos distintos.** Reemplaza la consecuencia «Certificado = copia firmada» del ADR de venta asistida. La copia firmada es la evidencia; el certificado, la constancia de cobertura.
2. **El asesor descarga el certificado de sus clientes.** HU22 dice «solo el titular» y HU39 dice que el asesor también puede: prevalece HU39 por ser la historia específica. Se limita a los clientes que el asesor atendió, con cada descarga auditada. Ruta propia, `GET /v1/suscripciones/asistidas/{id}/certificado`, sobre el mismo `ServicioCertificado`; la ruta del titular no cambia y sigue sin aceptar el JWT del asesor.
3. **La confirmación no depende del canal.** La firma asistida publica el mismo `poliza_emitida`, así que no se agrega nada para el primer correo.
4. **El reenvío va por evento.** `POST …/asistidas/{id}/confirmacion/reenvio` encola `confirmacion_reenvio_solicitada` en `solventa.polizas.ciclovida`, con la cola de salida. Suscripción ya tiene la relación asesor-cliente, la auditoría y el permiso de publicar; Notificaciones ya consume ese topic y sus reintentos se reutilizan. Una llamada directa exigiría una clave de servicio nueva y duplicaría los reintentos.
5. **El reenvío crea solo un correo,** con enlace de descarga nuevo. El push no se repite: ya avisó de la emisión. Cada reenvío tiene `event_id` propio, así que reentregar uno no duplica el correo.
6. **Espera entre correos (provisional).** 60 s en `reglas/v1.yaml`, responde 429. Se lee de `auditoria_asesor`, que se escribe después de encolar: es una protección contra el doble clic y no un límite estricto. Dos réplicas simultáneas podrían enviar dos correos, sin mayor daño.
7. **Firma por enlace (criterios 2 y 7).** `POST …/asistidas/{id}/firma/enlace` crea un enlace y encola `firma_enlace_solicitada`. El cliente firma sin sesión con el enlace y su código, sobre el mismo `ServicioFirma`: las reglas no se duplican. Evidencia: `canal = WEB`, IP y dispositivo del cliente, `modalidad = ASISTIDA_ENLACE` y `asesor_id` de quien lo envió.
8. **Token firmado con estado en base de datos, no un token aleatorio con hash.** Un token aleatorio en claro tendría que viajar por la cola de salida y Kafka, y guardarse en `envios` para los reintentos. Aquí el evento solo lleva el id y Notificaciones pide el token por una ruta interna al entregar, como con el certificado. La fila guarda lo que el token no puede expresar: `ACTIVO`, `REEMPLAZADO` o `USADO`. No se guarda ningún secreto.
9. **Clave derivada.** La clave de firma es un HMAC de `CERTIFICADOS_CLAVE_ENLACES` con el propósito `enlace-firma`. Un token de certificado no sirve para firmar, ni al revés, y no hace falta un secreto ni un cambio de infra.
10. **Fragmento y cabecera.** El correo apunta a `/<idioma>/sign#t=<token>` y la página envía el token en `X-Enlace-Firma`. El fragmento no llega al servidor, así que el token no queda en los logs de CloudFront ni del API Gateway, ni en el `Referer`.
11. **Un solo uso y reemplazo.** Un índice único parcial permite un solo enlace activo por suscripción; enviar otro reemplaza al anterior. El único uso lo garantiza la restricción de una firma por suscripción, incluso si el proceso cae antes de marcar el enlace. Uno usado solo permite ver que la póliza quedó firmada y repetir la firma con el mismo resultado, para que un corte de red no deje al cliente sin saber qué pasó.
12. **Plazo.** 24 h como máximo (confirmado para esta entrega) y nunca más allá del vencimiento de la firma.

## Riesgos aceptados para el prototipo

- **Mientras el OTP sea simulado (ADR-0019), el enlace por sí solo permite firmar:** el código es fijo. Sirve para demostrar el flujo, no para clientes reales.
- **Un enlace reenviado permite gastar los intentos.** Son por suscripción y tres códigos erróneos devuelven el cobro: con un OTP real, un tercero con el enlace podría anular la venta aunque no pueda firmar. Debe revisarse antes de usar un proveedor real.

## Consecuencias

- **Despliegue:** una versión anterior de Notificaciones ignora los eventos nuevos y los da por procesados, así que esos correos se perderían sin aviso. Notificaciones se despliega antes o a la vez que Suscripción.
- Siniestros también lee el topic e ignora los tipos desconocidos.
- El asesor sabe que el correo se solicitó, no que llegó. Los fallos los ve el operador en `GET /v1/notificaciones/envios-fallidos`.
- Sin cambios de infra: rutas, permisos y claves ya existen.
- **Migración `0006`:** con firmas `ASISTIDA_ENLACE` ya guardadas no se puede revertir la restricción de modalidad, porque la evidencia es inmutable.
- Si un enlace deja de estar activo mientras el correo se reintenta, el correo no sale y el envío termina `FALLIDO`, visible al operador.
- Alternativas descartadas: que `/v1/suscripciones/{id}/certificado` acepte los dos tokens (cambiaría el contrato de móvil y contradice el ADR de venta asistida); entregarle al asesor un enlace de `/v1/certificados/{token}` (sin auditoría de la descarga).
