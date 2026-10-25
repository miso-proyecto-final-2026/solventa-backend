# ADR-0014 — MS Pagos: cobro de la prima idempotente, conciliación y pasarela intercambiable

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-25
- **Historia:** HU18 (Sprint 2, F09). Requisitos RQ-S2 (EC-S2.1), RQ-D1 (EC-D1.2), RQ-M2 (EC-M2.2); HA05, HA08
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (Pagos dueño de `Transacción` y `Token_Tarjeta`; el PAN nunca se persiste), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (suscripción y cobro por Kafka), [ADR-0004](0004-patron-adaptadores-cambio-de-proveedor.md) (puerto, adaptador, selección por configuración y suite de contrato), [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (plantilla de eventos) y [ADR-0012](0012-ms-suscripcion-saga-decision-y-outbox.md) (saga y cola de salida)

## Contexto

La saga de suscripción (HU15) deja la suscripción en `APROBADA` / `COBRO` y publica `cobro_prima_solicitado` con el token
de la tarjeta. Hace falta el servicio que cobre. Cobrar es un efecto externo que **no se deshace**: el consumidor de la
plantilla reintenta y deshace su transacción si algo falla, y una pasarela puede cobrar y no responder. El requisito es
cero cobros duplicados y cero pérdida de cobros confirmados (EC-D1.2), con 0 datos de tarjeta en claro (EC-S2.1) y una
pasarela que se pueda cambiar tocando solo el adaptador (EC-M2.2).

## Decisión

1. **Kafka es el canal del cobro** (ADR-0002): Pagos consume `solventa.suscripcion.comandos` (grupo
   `solventa-pagos.cobros`) y responde en `solventa.pagos.resultados` con `cobro_prima_confirmado` o
   `cobro_prima_rechazado` (clave de partición `suscripcion_id`). `POST /v1/pagos/cobros` es una entrada interna con clave
   de servicio al **mismo** caso de uso, para pruebas y operación.
2. **Una clave, un cobro.** `transacciones.idempotency_key` es única; Suscripción la fija como `cobro_prima:<suscripcion_id>`
   (si un comando antiguo no la trae, Pagos la deriva igual). Una repetición no crea nada: se cuenta en `reentregas` (con un
   `UPDATE` atómico, para no perder cuentas entre réplicas) y, si el cobro ya terminó, se vuelve a anunciar el mismo resultado.
   La misma clave con otro monto o suscripción es un conflicto (409).
3. **Cobro en tres pasos, cada uno en su transacción:**
   - *registrar* (en la transacción del consumidor): la transacción `PENDIENTE`; **no** se llama a la pasarela aquí;
   - *cobrar*: el procesador toma la fila con `FOR UPDATE SKIP LOCKED`, anota el intento y **confirma antes de llamar**
     (si el proceso cae, queda rastro y la fila «prestada» vuelve a estar disponible al vencer el préstamo). La orden lleva
     la misma `idempotency_key`, así que tampoco la pasarela cobra dos veces;
   - *conciliar*: si un intento no tuvo respuesta cierta, el siguiente primero **consulta** la pasarela por la clave; si el
     cobro existe se conserva su resultado (`CONCILIADO`); si no, se vuelve a pedir con la misma clave.
   El resultado y su evento se guardan juntos (cola de salida, como en ADR-0012).
4. **Puerto `PasarelaPago`** (`cobrar`, `consultar`, `describir_token`) con tipos del dominio y tres errores:
   `PasarelaSinRespuesta` (pudo cobrar), `PasarelaNoDisponible` (no recibió la orden) y `TokenDesconocido`. El adaptador se
   elige con `PASARELA_PROVEEDOR` en `wiring.py`; una prueba de arquitectura verifica que solo `wiring.py` importa al
   simulador, y la suite `tests/contrato_pasarela.py` es obligatoria para cualquier adaptador nuevo. Las pruebas del caso de
   uso usan un doble del puerto, no el simulador: no cambian si cambia la pasarela.
5. **Pasarela simulada** con modos `APROBAR`, `RECHAZAR`, `TARDAR` y `PERDER_RESPUESTA` (por configuración o por prefijo
   del token). Su libro vive en la base (`simulador_cargos`) para que otra réplica pueda conciliar.
6. **SAQ-A:** Solventa solo recibe tokens. `POST /v1/pagos/tokenizar` registra un token (nunca el PAN) con la marca y los
   últimos 4 que informa la pasarela; la API y el consumidor rechazan cualquier texto con forma de PAN. El token vive solo
   en `tokens_tarjeta`; ni los eventos, ni la API, ni los logs lo llevan.
7. **Rechazo y período de gracia:** se registra el intento con código y motivo, la transacción queda `RECHAZADA` con
   `gracia_hasta = resuelta + PERIODO_GRACIA_DIAS`. **El valor de 3 días es provisional** (la historia no lo fija). Qué se
   hace dentro del período es de HU19. En la saga, el rechazo deja la suscripción en `APROBADA` / `COBRO` con el motivo
   visible en `GET /v1/suscripciones/{id}`; la confirmación la lleva a `COBRO_REALIZADO` / `FIRMA`.
8. **Periodicidad elegida por el cliente:** `POST /v1/suscripciones` acepta `periodicidad_pago` (`ANUAL` por defecto) y se
   cobra el primer período (prima anual o mensual). Los cobros recurrentes mensuales no tienen historia todavía.
9. **Logs JSON** con `trace_id` y `correlation_id` (el `trace_id` del comando viaja hasta el evento de resultado) y un filtro
   que enmascara PAN y tokens como segunda defensa.

## Consecuencias

- **Positivas:** cero duplicados por dos barreras independientes (restricción única en Pagos e idempotencia en la
  pasarela); ninguna caída deja un cobro sin rastro; cambiar de pasarela es un adaptador y una variable.
- **Negativas:** el procesador consulta la base cada medio segundo por réplica; una transacción sin respuesta de la
  pasarela queda `PENDIENTE` indefinidamente (no se da por perdida) y necesita una alerta y gestión humana (HU19).
- **Limitaciones conocidas:**
  - Un comando que agota sus reintentos llega a `….dlq` tal como vino, con el token (la plantilla no lo redacta).
  - El tráfico entre servicios dentro del clúster es HTTP sin TLS; el cifrado en tránsito cubre el borde, RDS
    (`rds.force_ssl`) y MSK (TLS). Pagos no usa S3.
  - Con una pasarela real, la conciliación depende de que exponga la consulta por clave de idempotencia; si no la tiene,
    el adaptador debe conciliar por la referencia del comercio.

## Alternativas descartadas

- **Llamar a la pasarela dentro del manejador del consumidor:** si algo falla después, la transacción se deshace pero el
  cobro no; una reentrega cobraría otra vez si la pasarela no fuera idempotente.
- **REST síncrono desde la saga:** contradice ADR-0002 y bloquea a Suscripción si Pagos o la pasarela están lentos.
- **Libro del simulador en memoria:** otra réplica no vería el cobro y no podría conciliar en staging.
- **Recibir el PAN y tokenizar en Pagos:** amplía el alcance PCI más allá de SAQ-A.
