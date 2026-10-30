# ADR-0020 — MS Suscripción: venta asistida por el asesor

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-30
- **Historia:** HU39 (Sprint 2, F10). Requisito RQ-I2
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (contextos acotados), [ADR-0008](0008-autenticacion-provisional-del-asesor-y-cotizacion-sin-cuenta.md) (token del asesor), [ADR-0014](0014-ms-pagos-cobro-idempotente-y-conciliacion.md) (SAQ-A) y [ADR-0019](0019-ms-suscripcion-firma-electronica-y-compensacion.md) (firma y compensación)

## Contexto

Las rutas de HU15 y HU21 solo aceptan el token del cliente. El asesor cotiza desde la consola (HU38), pero no había camino de esa
cotización a una suscripción: móvil no puede abrir una cotización hecha por el asesor. Tampoco existía en Suscripción la relación
entre un asesor y un cliente, ni un registro de lo que hace el asesor. HU22 (notificaciones) no existe, así que el envío por correo
no se puede construir todavía.

## Decisión

1. **Rutas propias, mismos servicios.** `/v1/suscripciones/asistidas` con el JWT del asesor. La tarjeta proponía un «modo asistida» en
   los endpoints de HU21, pero el cliente usa un token opaco validado por Identidad y el asesor un JWT: mezclarlos cambiaría el
   contrato de móvil. Debajo se usan `ServicioSuscripcion` y `ServicioFirma`; las reglas no se duplican.
2. **No bajo `/v1/asesor`.** Ese prefijo apunta a Cotización en el API Gateway, que enruta cada prefijo a un solo servicio.
   `/v1/suscripciones` ya está publicado para Suscripción. El enrutador se registra antes que `/{suscripcion_id}`.
3. **«Atendido por él» se hereda de la cotización.** Cotización expone `asesor_id` y `sin_cuenta`; Suscripción copia `asesor_id` a la saga
   al crearla. Quien no sea ese asesor recibe 403. Las sagas anteriores quedan con el campo nulo y ningún asesor las ve: un relleno
   tendría que cruzar esquemas (ADR-0001).
4. **El asesor inicia la saga de su cotización, con el cliente presente.** El titular es el de la cotización. Una cotización «sin
   cuenta» responde 409 `COTIZACION_SIN_CUENTA`: no hay cliente que pueda firmar.
5. **Pago: solo llega el token.** La consola genera en el navegador un token simulado, como móvil (`Pago.kt`). Como defensa adicional, la
   ruta rechaza un `metodo_pago` con forma de número de tarjeta. Con un proveedor real, ese componente se reemplaza por su formulario
   alojado; una tarjeta real digitada en la consola no cumpliría la regla de HU19 de que el asesor nunca la vea.
6. **`acepta_terminos` es una declaración del asesor (provisional).** Declara que el cliente está al tanto de la transacción y aceptó
   los términos, y queda auditada. No prueba la aceptación del cliente: esa prueba es su firma con OTP y, sin firma, el cobro se
   devuelve (ADR-0019). El cobro ocurre antes de firmar, por lo que esta decisión debe revisarse antes de usar un proveedor real. El
   asesor sigue sin poder otorgar el consentimiento de datos a nombre del cliente (ADR-0008).
7. **Misma evidencia, más el asesor.** La firma asistida usa `canal = WEB`, `modalidad = ASISTIDA_PANTALLA` y `asesor_id`; el firmante sigue
   siendo el cliente y la constancia del PDF dice quién asistió. Si el cliente ya firmó, se devuelve su firma, sin sobrescribirla.
8. **Auditoría inmutable.** `auditoria_asesor` registra asesor, fecha, suscripción (o cotización, si el inicio se rechazó), acción y
   resultado, también de los intentos rechazados. Guarda solo el código del error; nunca el OTP, el token ni datos personales. Un
   disparador de PostgreSQL rechaza `UPDATE` y `DELETE`. Las consultas de lista y detalle no se auditan.
9. **`puede_gestionar_firma` lo calcula el backend.** Exige `COBRO_REALIZADO` / `FIRMA`, sin compensación, con el documento generado y
   dentro del plazo. El estado no basta: con un reembolso pedido la saga sigue en `COBRO_REALIZADO` (ADR-0019).
10. **El cliente, con lo mínimo.** La lista lee `resumen_cliente` (HU20) y expone nombre, inicial del apellido y documento enmascarado, sin
    datos de contacto (ADR-0008). Si Identidad falla, la fila se muestra sin cliente.

## Consecuencias

- `GET /v1/cotizaciones/{id}` no tiene autenticación, así que `asesor_id` queda expuesto: es un UUID opaco. Se acepta para el prototipo;
  la alternativa es un endpoint interno con otra clave de servicio.
- **Infra:** Suscripción necesita `ASESOR_JWT_SECRETO`, el mismo de Identidad, Cotización y Pagos.
- **Certificado = copia firmada** (`firma/documento`): nada define un certificado aparte. HU22 podría cambiarlo.
- **Pendiente con HU22:** enlace de firma de un solo uso, página pública, envío por correo y reenvío del certificado.
- El OTP sigue simulado (ADR-0019): la firma en pantalla aún no prueba que el cliente esté presente.
- Alternativas descartadas: tabla de asignación asesor-cliente (sin requisito ni pantalla que la administre); reusar un token ya
  registrado en Pagos (un cliente nuevo no tiene).
