# HA08 · Pago paramétrico idempotente por eventos (EC-D1.1)

**Veredicto: hipótesis confirmada en el alcance del experimento.** En 7 escenarios (225 pagos distintos; solo en el primero, 200
duplicados inyectados y 250 mensajes releídos después) hubo **0 pagos duplicados y 0 pagos perdidos**: los pagos ejecutados en la pasarela fueron siempre
iguales a los pares evento–póliza distintos. El indicador de disponibilidad de la corrida corta fue del 100 %, pero no alcanza
para afirmar el 99,99 % (ver «Qué no demuestra»).

- **Código:** `services/pagos/tests/resiliencia/test_ha08_pago_idempotente.py` (marca `contenedores`: Kafka y PostgreSQL reales).
- **Evidencia cruda:** [`HA08/evidencia-cruda.json`](HA08/evidencia-cruda.json).
- **Reproducir:** `HA08_EVIDENCIA=docs/evidencias/HA08 pytest tests/resiliencia -s` en `services/pagos` (Docker encendido, unos 12 s).
- **Fecha de la corrida:** 2026-11-13, sobre `main` con HU29 (ADR-0022).

## Montaje

Un Kafka (`apache/kafka:3.9.0`, un nodo) y un PostgreSQL 16 en contenedores, con el esquema creado por Alembic. La pasarela es el
simulador de HU29, cuyo libro vive en la base: `pasarela.pagos()` cuenta los pagos ejecutados **de verdad**, y esa es la cifra que se
compara con los pares distintos. Los comandos son los que publica Siniestros (`event_id` derivado del par, ADR-0018), con 3 particiones.

Tres defensas en capas, de afuera hacia adentro:

1. **Consumidor:** `eventos_procesados` por grupo y `event_id`; una reentrega se omite y se cuenta.
2. **Servicio:** restricción única sobre `idempotency_key` y sobre `(evento_id, poliza_id)`; una repetición con otro sobre se cuenta y no paga.
3. **Pasarela:** el pago lleva la misma `idempotency_key` y, antes de reintentar, se consulta si ya pagó.

## Resultados por criterio

| # | Criterio | Escenario y cifras | Resultado |
|---|---|---|---|
| 1 | Un pago por par | 50 pares, 250 mensajes (cada par 1 original + 3 reentregas + 1 reenvío con otro sobre). Pagos en la pasarela **50** = pares **50** = intentos de pago **50** | ✅ |
| 2 | Reentrega registrada | 150 reentregas del consumidor + 50 del servicio = **200** duplicados inyectados, los 200 contados; ninguno volvió a pagar | ✅ |
| 3a | Consumidor muere antes de confirmar | La transacción se deshizo (0 indemnizaciones, 0 pagos). Al volver: 12 pagos para 12 pares | ✅ |
| 3b | Consumidor muere tras confirmar la base y antes del offset | Ya había 1 registrada. Al volver el mensaje se reentregó, se omitió y quedó contado (1). 12 pagos para 12 pares | ✅ |
| 3c | Procesador muere después de que la pasarela pagó | Tras la muerte: 1 pago ejecutado y 0 registrados. Al volver, el intento 2 **concilió** (`EN_CURSO, CONCILIADO`): 1 pago, 1 registrado | ✅ |
| 4 | Reinicio de lectura | Reposicionando el grupo al inicio se releyeron los 250 mensajes: reentregas del consumidor 150 → 400, **pagos 50 → 50** | ✅ |
| 5 | Concurrencia | 3 réplicas leyendo el mismo flujo a la vez (90 lecturas de 30 pares): **30** pagos, 60 reentregas del servicio | ✅ |
| 6 | Fallo transitorio | La pasarela falló 2 veces por cada uno de 20 pares (40 fallos): 60 intentos, misma clave, **20** pagos | ✅ |
| 7 | Disponibilidad | 100 comandos con 20 ms entre sí: 100 resueltos en ≤ 5 s, 0 errores no controlados (**100 %**) | ⚠️ ver abajo |
| 8 | Evidencia cruda | Eventos enviados, duplicados, pagos creados, reentregas, reinicios y estado del grupo (offset confirmado, fin y retraso por partición) en el JSON | ✅ |

## Qué no demuestra

- **Disponibilidad ≥ 99,99 % (criterio 7).** El criterio estaba «a confirmar». Con 100 comandos la resolución es del 1 %: sirve para ver que
  el flujo no se cae, no para medir cuatro nueves. Medirlo exige una corrida larga o de carga (el plazo de 5 s también es una propuesta).
- **Las muertes son de proceso, no de pod.** Se simulan cancelando la tarea del consumidor y haciendo fallar la confirmación del offset;
  no se mata un pod en EKS ni se reasignan particiones entre consumidores del mismo grupo (el escenario 5 usa un grupo por réplica).
  Eso queda para una corrida en staging con MSK.
- **Un solo nodo de Kafka, sin MSK ni IAM**, y una pasarela simulada: si una pasarela real no fuera idempotente por clave, la tercera defensa no valdría.
- **Los intentos agotados.** Si la pasarela real nunca responde a la consulta, una indemnización puede quedar `FALLIDA` habiendo pagado
  (ADR-0022): queda como evidencia para conciliar en HU31, pero no hay reintento manual.

## Hallazgo del experimento

La primera corrida perdió pagos: la cuenta simulada por defecto salía de un hash hexadecimal y, con 13 o más dígitos seguidos, la propia
validación anti-tarjeta rechazaba el comando, que iba a la cola de fallidos sin pagarse. Se corrigió dentro del PR #75 de HU29
(`cuenta_por_defecto` en `indemnizaciones.py`) con un test de regresión de 20.000 pólizas.

## Decisiones derivadas

1. **Mantener las tres defensas.** Cada una cubre un hueco que las otras no: sin la del consumidor los reenvíos con otro sobre llegan al
   servicio; sin la del servicio una condición de carrera entre réplicas pagaría dos veces; sin la de la pasarela, 3c pagaría dos veces.
2. **Dejar el arnés en CI** (12 s con Docker): es la prueba de EC-D1.1 ante cualquier cambio en el consumidor o el pago.
3. **Repetirlo en staging con MSK** (matar un pod de Pagos, reasignar particiones) antes de cerrar el requisito, y medir la disponibilidad
   en una corrida larga cuando exista la prueba de carga (S7).
4. **No reintentar a mano los `FALLIDA`** hasta HU31: primero consultar a la pasarela por la clave.
