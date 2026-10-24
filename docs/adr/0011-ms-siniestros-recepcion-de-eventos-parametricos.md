# ADR-0011 — MS Siniestros: recepción de eventos paramétricos sobre el cliente de eventos de la plantilla

- **Estado:** Propuesto (lista de tipos de evento y clave de partición pendientes de confirmar con el equipo)
- **Fecha:** 2026-10-23
- **Historia:** HU27 (E07 · F16 Recepción de eventos paramétricos, Sprint 2)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (MS Siniestros posee `Evento_Paramétrico`; referencia pólizas solo por id), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (pago paramétrico asíncrono, entrega al menos una vez, consumidores idempotentes) y [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (MSK y plantilla de cliente por servicio, `docs/plantillas/eventos/`).

## Contexto

HU27 crea MS Siniestros con el endpoint por el que un proveedor de vuelos o clima entrega eventos. Debe responder de inmediato, no perder eventos ante ráfagas ni ante la caída del consumidor, y no duplicarlos si el proveedor reintenta. HU28, que valida la condición de cada póliza, consumirá lo que aquí se publique.

## Decisión

1. **Se usa la plantilla de eventos (HS2) tal cual, copiada en `app/eventos/`** (con sus pruebas en `tests/eventos/`, como pide su README). No hay bus propio: el servicio publica con `PublicadorKafka` y consume con `ConsumidorEventos`. En pruebas unitarias se usa `PublicadorMemoria`; en desarrollo sin `KAFKA_BOOTSTRAP_SERVERS` el servicio arranca con ese publicador y **sin consumidor** (solo con `SOLVENTA_MODO_DESARROLLO=1`; fuera de desarrollo, falla cerrado).
2. **Sobre.** Cada evento recibido se publica en `solventa.siniestros.eventos` como `evento_parametrico_recibido` (contrato de `solventa-infra`), con `source=ms-siniestros`, `event_id` igual al `evento_uuid` interno, `correlation_id` de la solicitud y el evento completo en `payload`.
3. **Orden de las operaciones al recibir:** autenticar → validar → registrar (idempotente) → publicar → acusar. El acuse es `202` con `evento_uuid` y no espera al procesamiento. Un reenvío del mismo `(fuente, evento_id)` responde `200` con `duplicado: true`, sin segundo registro ni segunda publicación (restricción única en la base y una sola fila en `publicaciones_bus`).
4. **No perder eventos con Kafka caído o lento.** El evento se registra **antes** de publicarse. Si la publicación falla (`KafkaError`, tiempo agotado), se responde `503 BUS_SATURADO` con `Retry-After: 1`; el evento ya está guardado y, cuando el proveedor lo reenvía con el mismo `evento_id`, solo falta publicarlo. Es el caso de doble escritura que ADR-0010 deja abierto: aquí se resuelve del lado de la recepción, porque la base va primero y el proveedor reintenta; no hace falta *outbox*.
5. **Consumidor.** Grupo `solventa-siniestros.recepcion` (debe empezar por el nombre del servicio, por la política de IAM). Idempotencia, reintentos de retroceso exponencial (1 s base, tope 30 s, 5 intentos) y cola de fallidos las pone la plantilla. El manejador de este sprint no hace nada más: la reserva de `(grupo, event_id)` en `eventos_procesados` es la constancia de que el evento llegó al final. HU28 agrega la evaluación de la condición escribiendo con la misma sesión.
6. **Autenticación y fuente.** `X-Service-Key` con pares `fuente=clave` en `SINIESTROS_API_KEYS`. Sin llave o con una inválida: `401` y no se registra nada. El campo `fuente` del evento debe ser la de su llave; otra distinta es `400 FUENTE_DESCONOCIDA` (un proveedor no puede hablar por otro). Vacía = nadie accede.
7. **Validación y rechazos (`400`).** Campos obligatorios, tipo dentro del catálogo, fecha ISO 8601 con zona y no futura (tolerancia 5 min), `datos` objeto no vacío de hasta 8 KB. Cada rechazo se guarda en `eventos_rechazados` con código y campos defectuosos, nunca con el valor recibido.
8. **Persistencia (`siniestros`).** De solo inserción, con triggers de PostgreSQL que rechazan UPDATE y DELETE: `eventos_parametricos` (fecha de recepción, fuente, `estado_validacion`), `eventos_rechazados` y `publicaciones_bus`. `eventos_procesados` es la de la plantilla (se actualiza: cuenta reentregas). Sin llaves foráneas hacia otros servicios.
9. **Partición.** La clave es `poliza_id` si el evento la trae (es la del contrato del topic) y, si no, `evento_id`. Los eventos de proveedores suelen describir un fenómeno, no una póliza; el orden por póliza lo necesitará HU28 al repartir un evento entre las pólizas afectadas.
10. **Verificación.** `GET /v1/siniestros/eventos/resumen` devuelve recibidos, rechazados, publicados, procesados y reentregas, para comparar contra lo enviado en una ráfaga.

## Consecuencias

- **Prueba contra Kafka real.** `tests/siniestros/test_kafka.py` (testcontainers) recibe 30 eventos duplicados con el consumidor caído, lo levanta, lo vuelve a tirar, envía 10 más y verifica 40 recibidos = 40 publicados = 40 procesados, 0 reentregas. Cubre los criterios 5 y 7 contra un broker real; **no está demostrada la meta de 1.000.000 en 10 min (criterio 6)**: eso exige staging con MSK y PostgreSQL.
- **Costo por evento:** dos escrituras en línea (evento y publicación) más la reserva del consumidor. Si la ráfaga lo exige, se agrupan en lote o se saca la escritura de publicación del camino crítico. La sesión de SQLAlchemy del consumidor es síncrona dentro del bucle asíncrono (limitación de la plantilla).
- **Despliegue pendiente.** El servicio debe declararse en `solventa-infra` (`services.yaml`) con `edge`, `eventos: {produce: [solventa.siniestros.eventos], consume: [solventa.siniestros.eventos]}` y el secreto de `SINIESTROS_API_KEYS`; sin eso no recibe `KAFKA_BOOTSTRAP_SERVERS` ni permisos.
- **Pendiente de confirmar:** lista final de tipos de evento (se aceptan vuelos y clima del enunciado y sismo, inundación y sequía de la documentación técnica), campos requeridos por tipo (los define HU28 con las reglas) y si el proveedor real puede reintentar con el mismo identificador.
