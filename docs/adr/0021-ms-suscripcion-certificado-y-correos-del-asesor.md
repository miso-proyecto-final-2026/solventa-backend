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

## Consecuencias

- **Despliegue:** una versión anterior de Notificaciones ignora el evento nuevo y lo da por procesado, así que el correo se perdería sin aviso. Notificaciones se despliega antes o a la vez que Suscripción.
- Siniestros también lee el topic e ignora los tipos desconocidos.
- El asesor sabe que el correo se solicitó, no que llegó. Los fallos los ve el operador en `GET /v1/notificaciones/envios-fallidos`.
- Sin cambios de infra: rutas, permisos y claves ya existen.
- Alternativas descartadas: que `/v1/suscripciones/{id}/certificado` acepte los dos tokens (cambiaría el contrato de móvil y contradice el ADR de venta asistida); entregarle al asesor un enlace de `/v1/certificados/{token}` (sin auditoría de la descarga).
