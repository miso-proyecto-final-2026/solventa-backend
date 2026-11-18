# ADR-0022: MS Pagos — pago automático de la indemnización paramétrica

- **Estado:** Aceptado (HU29, Sprint 3)
- **Depende de:** [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (pago paramétrico asíncrono con `idempotency_key`),
  [ADR-0004](0004-patron-adaptadores-cambio-de-proveedor.md) (pasarela detrás de un puerto),
  [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (plantilla de eventos),
  [ADR-0014](0014-ms-pagos-cobro-idempotente-y-conciliacion.md) (cobro idempotente y conciliación) y
  [ADR-0018](0018-ms-siniestros-validacion-de-condicion-parametrica.md) (el comando `indemnizacion_solicitada`)

## Contexto

HU28 deja `indemnizacion_solicitada` en `solventa.siniestros.comandos` y nadie lo consumía. El ADR-0001 asigna HU29 a Siniestros y la
documentación técnica pone el pago en Pagos: la historia pedía confirmar quién ejecuta el pago.

## Decisión

1. **Pagos ejecuta el pago; Siniestros solo lo pide.** Pagos es el único servicio que habla con la pasarela (ADR-0004).
2. **Mismo patrón que el cobro de la prima** (ADR-0014), en tablas propias: `indemnizaciones` (una por par evento–póliza, con restricción
   única sobre `idempotency_key` y sobre `(evento_id, poliza_id)`) e `intentos_pago`. Registrar, pagar y conciliar son tres pasos en
   transacciones distintas; la orden a la pasarela lleva la misma clave y antes de reintentar se consulta si ya pagó.
3. **Puerto `PasarelaPago.pagar`** con adaptador simulado. El libro del simulador (`simulador_cargos`) distingue `PAGO` de `COBRO`.
4. **Un segundo consumidor** en el grupo `solventa-pagos.indemnizaciones` (la política de IAM exige el prefijo del servicio) sobre
   `solventa.siniestros.comandos`. El resultado sale por la cola de salida a `solventa.pagos.indemnizaciones`, clave `evento_id`:
   `indemnizacion_pagada` o `indemnizacion_fallida`.
5. **Fallos.** Una respuesta que no llega se reintenta con retroceso exponencial hasta `PAGOS_INDEMNIZACION_MAX_INTENTOS` (5). Agotados los
   intentos, o ante un rechazo, la indemnización queda `FALLIDA` (estado final, visible en la tabla y en el evento) y no se pierde.
6. **Cuenta de destino tokenizada.** Es un token `cta_…`; no se publica ni se registra. Un valor con forma de tarjeta se rechaza.

## Consecuencias y a confirmar

- **S5, cuenta de destino:** ninguna historia captura la cuenta del cliente. Mientras tanto, si el comando no trae `cuenta_destino`, se
  paga a una cuenta simulada opaca derivada de la póliza. Debe sustituirse cuando exista el origen real de la cuenta.
- **El comando no trae `cliente_id`** (Siniestros referencia pólizas por id). El evento de resultado lo propaga si llega y, si no, va nulo.
- **`FALLIDA` por intentos agotados puede haber pagado** si la pasarela real nunca responde a la consulta. El estado y los intentos quedan
  como evidencia para conciliar (HU31); no hay un reintento manual todavía.
- **Infra:** `solventa-pagos` debe consumir `solventa.siniestros.comandos` y publicar en `solventa.pagos.indemnizaciones` (cambio en
  `services.yaml` de `solventa-infra`).
