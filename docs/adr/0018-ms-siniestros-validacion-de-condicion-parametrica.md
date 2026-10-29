# ADR-0018 — MS Siniestros: validación de la condición paramétrica

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-29
- **Historia:** HU28 (Sprint 2, F17). Requisitos RQ-E2, RQ-D1. HA relacionada: HA08
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (Siniestros referencia pólizas solo por id), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (pago paramétrico asíncrono con `idempotency_key`), [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (plantilla de eventos) y [ADR-0011](0011-ms-siniestros-recepcion-de-eventos-parametricos.md) (HU27 deja el evento registrado y publicado)

## Contexto

HU27 publica cada evento en `solventa.siniestros.eventos` y su consumidor no hace nada más que dejar constancia. HU28 debe decidir,
para cada póliza paramétrica activa afectada, si el evento cumple su condición de disparo y, si es así, pedir la indemnización.
Tres cosas no están definidas en los documentos: **qué productos son paramétricos**, **cómo es la condición de disparo** y **cómo
sabe Siniestros qué pólizas están activas** sin leer la base de Suscripción (ADR-0001).

## Decisión

1. **Copia local de pólizas, alimentada por eventos.** Siniestros consume `solventa.polizas.ciclovida` en el grupo
   `solventa-siniestros.polizas` y guarda en `siniestros.polizas_parametricas` solo lo que necesita: id, producto, zona, vigencia y
   fecha de cancelación. `poliza_emitida` la crea o actualiza y `poliza_cancelada` marca la cancelación. Las pólizas de productos
   sin reglas se ignoran. Sin llaves foráneas ni llamadas síncronas a Suscripción: si esta cae, la evaluación sigue.
2. **Evaluación en el consumidor de HU27.** El manejador del grupo `solventa-siniestros.recepcion` pasa a ser `Evaluador`. Con
   `poliza_id` evalúa esa póliza; sin él, recorre las pólizas de los productos con regla para ese tipo de evento **por lotes de 500**
   con paginación por clave, así que un evento masivo no carga todo en memoria. La partición por `poliza_id` conserva el orden
   dentro de una póliza (criterio 9).
3. **Vigencia contra la fecha del evento, no la de hoy.** Se cubre si `vigencia_desde <= ocurrido_en <= fecha_vencimiento` y la
   póliza no estaba cancelada en ese momento. Una póliza vencida, cancelada o aún no vigente queda registrada como
   `NO_CORRESPONDE` con su motivo (criterio 4).
4. **Reglas en un archivo versionado** (`app/siniestros/reglas/parametricas.yaml`, criterio 8). Por producto y tipo de evento: una
   lista de condiciones `campo operador umbral` (todas deben cumplirse), `exige_zona` y el `monto_indemnizacion`. El archivo se
   valida al arrancar y un defecto impide el arranque. Cada evaluación guarda `version_reglas`.
5. **Resultado auditable** (criterio 7). `siniestros.evaluaciones_parametricas`, de solo inserción (trigger de PostgreSQL, como las
   tablas de HU27), guarda el resultado, el motivo, los **valores comparados** y la versión de las reglas, también cuando no se paga.
6. **Un solo pago por evento y póliza** (criterio 5), con dos defensas: la reserva de idempotencia del consumidor (una reentrega se
   omite y se cuenta en `reentregas`) y la restricción única `(evento_uuid, poliza_id)`, que cubre el caso de que el mismo par llegue
   por otro camino. El comando lleva `idempotency_key = <evento_uuid>:<poliza_id>` y un `event_id` derivado de ella (UUIDv5).
7. **Comando por el bus.** `indemnizacion_solicitada` en `solventa.siniestros.comandos`, clave de partición `evento_id` (contrato de
   infra), con el monto, la regla y la versión. Se publica **dentro de la transacción del manejador**: si Kafka falla, se deshacen la
   reserva y las evaluaciones y el consumidor reintenta (y agotados los intentos, va a `.dlq`). El único riesgo es que el proceso caiga
   entre publicar y confirmar, y entonces el comando sale dos veces; MS Pagos lo descarta por la `idempotency_key`.

## A confirmar con producto

- **Productos paramétricos.** El catálogo de Cotización no tiene ninguno. El archivo propone `parametrico_clima` y
  `parametrico_vuelo`; no existen aún como productos vendibles, así que hoy nada alimenta la copia de pólizas en staging.
- **Umbrales y montos** del archivo de reglas (sismo ≥ 6,0; inundación ≥ 150 cm; sequía ≤ 20 mm; tormenta ≥ 90 km/h; vuelo ≥ 180 min).
- **La zona asegurada.** `poliza_emitida` hoy no la trae: el registro lee un campo opcional `zona`. Suscripción tendría que
  incluirlo (y el monto asegurado, si la indemnización debe depender de él) para que una póliza real tenga cobertura geográfica.
- **`poliza_cancelada`** aún no lo publica nadie; el registro ya lo entiende.
- **Consumidor de comandos.** MS Pagos todavía no consume `solventa.siniestros.comandos` (HU29/HU30).

## Consecuencias

- **Positivas:** Siniestros no se acopla a Suscripción; cada decisión se puede reconstruir; cambiar umbrales no toca el código;
  el procesamiento por lotes acota la memoria.
- **Negativas:** la copia de pólizas es eventualmente consistente (una emisión o cancelación muy reciente puede no haber llegado);
  una póliza emitida antes de este despliegue no está en la copia hasta que se reemita su evento; hasta confirmar los puntos de
  arriba, los umbrales son valores iniciales.
- **Infra:** `solventa-siniestros` pasa a consumir `solventa.polizas.ciclovida` y a publicar en `solventa.siniestros.comandos`
  (`environments/staging/services.yaml`, `solventa-infra`).
