# ADR-0023: MS Pagos — registro y conciliación de desembolsos de indemnizaciones

- **Estado:** Aceptado (HU31, Sprint 3)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin claves foráneas entre servicios),
  [ADR-0008](0008-autenticacion-provisional-del-asesor-y-cotizacion-sin-cuenta.md) (JWT del personal) y
  [ADR-0022](0022-ms-pagos-pago-automatico-de-indemnizacion.md) (el pago que se concilia)

## Contexto

Operaciones necesita ver cada desembolso con el evento y la póliza que lo originaron, comprobar que no hubo pagos duplicados y
verificarlo contra la pasarela. Pagos no puede unir tablas con Siniestros.

## Decisión

1. **Copia en el comando.** Siniestros agrega al comando `indemnizacion_solicitada` tres objetos opcionales: `evento` (tipo, fecha, ubicación,
   magnitud = los valores que se compararon), `poliza` (producto, zona) y `validacion` (resultado, motivo, regla, versión). Pagos los
   guarda en `pagos.indemnizaciones` (columnas filtrables `tipo_evento`, `evento_ocurrido_en`, `ubicacion` y JSON para el resto). Son
   opcionales: un comando anterior se paga igual y queda sin esos datos.
2. **Estado de conciliación** `PENDIENTE` → `CONCILIADO` | `CON_DIFERENCIA`, separado del estado del pago. Solo se concilian los pagados.
3. **Verificar contra la pasarela** es consultar su extracto (`PasarelaPago.extracto`), hoy el libro del simulador. Coinciden monto, moneda y
   referencia → `CONCILIADO` con quién y cuándo. Monto distinto → `CON_DIFERENCIA` con lo que dijo la pasarela. Sin pago en el extracto → 409 y
   no cambia nada. Se puede conciliar de nuevo tras corregir; eso borra el escalamiento.
4. **Escalar** solo con diferencia, con nota obligatoria (hasta 500 caracteres); queda quién y cuándo.
5. **Acceso** solo para el rol OPERADOR. El cliente se muestra enmascarado y nunca la cuenta de destino.
6. **Duplicados.** `GET /resumen` cuenta las claves con más de un pago pagado; la restricción única de HU29 la mantiene en 0, y la vista lo muestra.
7. **Exportar** a CSV (con BOM para Excel y fórmulas neutralizadas) y a PDF (`reportlab`, hasta 10.000 filas).

## A confirmar y límites

- **Número de póliza, cliente y monto asegurado.** Siniestros no los tiene (referencia pólizas por id). El comando propaga `cliente_id` si llega;
  `numero` y `monto_asegurado` se leen de `poliza` si algún día vienen. Hasta entonces la vista los muestra vacíos.
- **Extracto simulado.** Con una pasarela real habrá que implementar `extracto` contra su API o su archivo de conciliación.
- **Enmascarado del cliente** (`cli***78`) está a confirmar con negocio.
- **Borde público.** El prefijo `/v1/pagos/conciliacion` aún no se publica; se abre en `solventa-infra` cuando la consola lo consuma. La pantalla
  «Operaciones > Siniestros > Conciliación», los textos es-CO/en-US y la accesibilidad son de `solventa-web` y quedan fuera de este cambio.
