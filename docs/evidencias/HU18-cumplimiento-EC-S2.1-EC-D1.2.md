# HU18 — Informe de cumplimiento de EC-S2.1 y EC-D1.2 (insumo de HA05)

- **Historia:** HU18, cobro de la prima con pasarela (MS Pagos), Sprint 2
- **Fecha:** 2026-10-25
- **Alcance de la evidencia:** pruebas automatizadas en local (Windows, Python 3.11, PostgreSQL 16 y Kafka 3.9 reales en
  contenedores) y una prueba de humo con el servicio en ejecución. **Todavía no hay evidencia en staging**: falta desplegar
  MS Pagos y aplicar infra (lo hace una persona). Este informe se actualiza cuando exista.
- **Decisión de diseño:** [ADR-0014](../adr/0014-ms-pagos-cobro-idempotente-y-conciliacion.md)

## EC-S2.1 — 0 datos de tarjeta en claro en almacenamiento y en logs

**Cómo se garantiza**

1. Solventa no recibe el PAN (flujo SAQ-A): la API (`/tokenizar`, `/cobros`) y el consumidor de Kafka rechazan cualquier
   texto con forma de número de tarjeta (13 a 19 dígitos, con o sin separadores) y no devuelven el valor recibido.
2. El token de la pasarela se guarda solo en `pagos.tokens_tarjeta.token`, con marca y últimos 4. Ni los eventos de
   resultado, ni la API, ni los logs lo llevan.
3. Logs en JSON con un filtro que enmascara cualquier PAN o token que llegue a un log (segunda defensa).

**Evidencia (resultado real)**

| Prueba | Qué verifica | Resultado |
|---|---|---|
| `services/pagos/tests/test_seguridad.py::test_cero_datos_de_tarjeta_en_logs_y_en_la_base` | Recorre cobro aprobado, rechazado, respuesta perdida, API, y 3 PAN de prueba enviados por Kafka y por API. Luego busca en **todos los logs** y en **todas las columnas de todas las tablas** números que pasen Luhn y los tokens usados. | pasa: 0 PAN en logs, 0 PAN en la base, 0 tokens en logs; el token solo aparece en `tokens_tarjeta.token` |
| `test_seguridad.py` (otras 9) | El filtro enmascara PAN (con y sin separadores) y tokens, también en trazas de excepción; el log es JSON con `trace_id` y `correlation_id`. | 9 pasan |
| `test_api.py` (PAN en `tokenizar` y en `metodo_pago`) | 422 `DATO_DE_TARJETA_NO_PERMITIDO` sin eco del dato; la pasarela no recibe la orden. | pasan |
| Mutación: registrar el token en un log de `servicio.py` | La prueba de seguridad debe fallar. | falló (detectada) y se revirtió |
| Prueba de humo local (uvicorn + Newman, 12 peticiones, 25 aserciones) | `grep` sobre el log del proceso. | 0 apariciones de `tok_sim_` y 0 de `4111` en 28 líneas de log JSON |

**Limitaciones**

- Un comando de cobro que agota sus reintentos llega a `solventa.suscripcion.comandos.dlq` tal como vino, con el token (la
  plantilla de eventos no redacta la cola de fallidos). Tiene los mismos permisos y retención que el topic de origen.
- El token de la pasarela sí se almacena (lo exige la historia, para cobrar). El cifrado en reposo es el de RDS.

## EC-D1.2 — Cero pérdida de transacciones confirmadas y cero cobros duplicados

**Cómo se garantiza**

1. `transacciones.idempotency_key` es única (una saga, un cobro: `cobro_prima:<suscripcion_id>`).
2. Se anota el intento y se confirma en la base **antes** de llamar a la pasarela; la orden lleva la misma clave, así que
   la pasarela tampoco cobra dos veces.
3. Sin respuesta cierta, el siguiente intento **consulta** la pasarela por la clave antes de volver a cobrar (conciliación).
   Una transacción nunca se da por perdida: queda `PENDIENTE` hasta tener respuesta.
4. Resultado y evento se guardan en la misma transacción (outbox); los consumidores son idempotentes por `event_id`.

**Evidencia (resultado real)**

| Prueba | Escenario | Resultado |
|---|---|---|
| `test_postgres.py::test_el_mismo_comando_en_varias_replicas_deja_una_sola_transaccion_y_un_solo_cargo` | 8 réplicas (hilos con su propio servicio y sesión) reciben a la vez el mismo comando con distinto `event_id`, contra PostgreSQL real. | pasa: 1 transacción, 1 token, 1 cargo en la pasarela, 7 reentregas registradas |
| `test_postgres.py::test_varias_replicas_procesando_la_misma_transaccion_hacen_un_solo_intento` | 8 réplicas intentan cobrar la misma transacción. | pasa: 1 intento, 1 cargo |
| `test_postgres.py::test_respuesta_perdida_conciliada_por_otra_replica_no_duplica_el_cargo` | La réplica 1 cobra y pierde la respuesta; la réplica 2 concilia. | pasa: `SIN_RESPUESTA` → `CONCILIADO`, 1 cargo |
| `test_servicio.py` (respuesta perdida, pasarela lenta, pasarela caída, caída del proceso a mitad del cobro) | Los cuatro caminos sin respuesta cierta. | pasan: en todos, 1 cargo y transacción `APROBADA` |
| `tests/eventos/test_cobros_kafka.py` | Kafka real: el mismo evento entregado dos veces; comando inválido con 5 reintentos (1, 2, 4, 8 s) y envío a `.dlq`. | pasan: 1 transacción, 1 cargo, resultado publicado; DLQ con `intentos=5` |
| Contratos de eventos con MS Suscripción (`tests/contratos/`) | La saga llega a `COBRO_REALIZADO` con `cobro_prima_confirmado` y queda en `COBRO` con motivo y gracia con `cobro_prima_rechazado`. | pasan en los dos servicios |
| Repetición de la carrera y de Kafka | 3 corridas seguidas. | 10/10 en las 3 |
| Prueba de humo local | Cobro con `tok_sim_perder_…`: el servicio vivo concilió solo. | `SIN_RESPUESTA,CONCILIADO`, 1 fila en el libro de la pasarela |

**Defectos encontrados por estas pruebas y corregidos antes de entregar**

- La réplica que perdía la carrera por la clave fallaba (`UnmappedInstanceError` al descartar un objeto que la sesión ya
  había descartado). Lo detectó una prueba unitaria escrita para ese camino.
- Con 8 réplicas en PostgreSQL real, el contador de reentregas perdía actualizaciones (lectura-modificación-escritura); se
  pasó a un `UPDATE` atómico. No afectaba la garantía de un solo cobro, pero sí el registro de reentregas que exige el DoD.

**Limitaciones**

- La pasarela es simulada. Con una real, la conciliación depende de que ofrezca consulta por clave de idempotencia.
- Una transacción que nunca obtiene respuesta queda `PENDIENTE` indefinidamente (sin pérdida, pero sin cierre): necesita
  una alerta y la gestión humana de HU19.
- `min.insync.replicas=1` en MSK (ADR-0010) admite perder un evento si el líder cae antes de replicar; la cola de salida
  lo reintenta desde la base, pero no cubre un evento ya confirmado por el broker y perdido después.

## Totales de las suites (local, con Docker)

| Servicio | Resultado | Cobertura `app/` | Cobertura módulos de negocio |
|---|---|---|---|
| MS Pagos | 160 pasan, 0 fallan | 98 % | 97 % (`app/pagos/*`) |
| MS Suscripción | 171 pasan, 0 fallan | 97 % | 98 % (`app/suscripcion/*`) |
