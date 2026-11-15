# ADR-0024 — Validación del perfilamiento en línea (HA02)

- **Estado:** Propuesta, **no aceptada**.
  - Fija cómo se valida HA02 y qué se acepta como evidencia.
  - La medición en staging del 2026-11-15 cumple c6 y c7 y no cumple c1–c5.
  - Se mantiene en Propuesta, con las decisiones 9 a 14, hasta una nueva medición.
- **Fecha:** 2026-11-14 (actualizado el 2026-11-15 con la medición en staging)
- **Historia:** HA02 (arquitectura, Sprint 3) — [issue #43](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/43)
- **Depende de:** [ADR-0009](0009-ms-perfilamiento-enriquecimiento-y-replica-de-proveedores.md) (MS Perfilamiento:
  consentimiento en cada operación, caché con TTL por tipo de dato, linaje y enmienda HU24). **Relacionado:**
  [ADR-0013](0013-cache-aside-circuit-breaker-latencia-cotizacion.md) (caché, breaker y meta de la cotización en línea)
  y [ADR-0015](0015-autoescalado-por-solicitudes-concurrentes.md) (método de medición sobre staging).
- **Experimento:** `solventa-experimentos/ha02/` (scripts y `RUNBOOK.md`).
- **Informe:** `ha02/resultados/INFORME.md` e `informe-ha02.json`.
- **Evidencia cruda:** el paquete de la sesión, fuera de git.

## Contexto

El informe de la semana 7 dejó HA02 «parcialmente validada» sobre un prototipo (20.002 perfilamientos por hora, 0 % de
fallos, 94,4 % de aciertos de caché). Desde entonces existe MS Perfilamiento real (HU23), que consulta el consentimiento
en MS Identidad en cada operación, escribe un perfil y dos filas de linaje por enriquecimiento y, con HU24, audita
quién consulta el linaje. La consecuencia de rendimiento del ADR-0009 quedó abierta: «no está medido».

HA02 pide, con el servicio real en staging: p95 ≤ 400 ms y p99 ≤ 800 ms por escenario (caché con y sin acierto, fuente
lenta), ≥ 20.000 perfilamientos por hora sin errores por saturación, que la cotización en línea conserve su meta durante
la carga, degradación controlada con una fuente lenta o caída, bloqueo sin llamadas a fuentes para quien no tiene
consentimiento (sin distorsionar la medición) y que, tras revocar, el perfil cacheado deje de usarse en ≤ 5 minutos.

Al planear la medición se encontró además que MS Cotización y MS Perfilamiento comparten la caché de proveedores: la
misma base de Redis (`/0`, la que pone `solventa-infra` a todos los servicios) y la misma clave
(`perfil:{cliente_id}:{fuente}`, porque `cache.py` se replicó tal cual).

## Decisión

1. **Qué se mide y dónde.** k6 en una EC2 dentro de la VPC de staging, sin reutilizar conexiones, contra el NLB interno
   (Perfilamiento por su NodePort, con `X-Service-Key`). El borde público queda fuera de la medición y se declara. Si
   para la sesión ya no existe el NodePort de Perfilamiento (borde TEMPORAL), k6 corre como Job dentro del clúster.
2. **Escenarios.** Caché sin acierto (clientes nunca enriquecidos), caché con acierto (grupo calentado), meseta de
   **60 min** a 21.600/h con un 5 % sin consentimiento, fuente lenta en dos variantes —**4a** 200–500 ms, bajo el
   timeout de 0,7 s, y **4b** 600–900 ms, alrededor del timeout—, fuente caída sin respaldo (valor por defecto) y con
   respaldo, solo clientes sin consentimiento, y revocación. Un escenario por corrida, con etiquetas por escenario y por
   consentimiento.
3. **Veredicto por criterio** (`analizar-ha02.py`): c1 en caché sin acierto, con acierto y 4a; c2 cuenta solo los 201
   de clientes con consentimiento en la ventana de la meseta y exige cero errores por saturación y cero iteraciones
   descartadas por el generador; en c3 la **evidencia principal es la diferencia de p95 de la cotización entre una línea
   base de 10 min (Cotización sola) y la meseta**, las dos con el mismo simulador por defecto (20–80 ms); el p95 absoluto
   se informa junto a la meta de RQ-L1 (250 ms) solo como referencia, porque **no es comparable con HA01**, que midió
   con Open Finance a 200–500 ms (ADR-0013); c4 exige 201 dentro de la latencia de c1 y, con la fuente caída, la marca
   `degradado` en el 100 % (en 4b solo la llevan las que vencieron el timeout, así que no se exige). Un criterio sin su
   evidencia queda «sin datos», nunca «cumple».
4. **Los bloqueos no distorsionan la medición.** La latencia de c1 y c2 se calcula solo con clientes con consentimiento;
   los 403 se informan aparte.
5. **Evidencia de «sin llamar a ninguna fuente» (c5): indirecta.** No se agrega un contador de llamadas a los
   simuladores. Se acepta como prueba que cada 403 deja su intento en `intentos_bloqueados` (origen `carga`, que debe
   igualar los 403 de k6) y que ningún cliente sin consentimiento tiene perfil: el perfil y su linaje se escriben después
   de consultar las fuentes, en una sola transacción (ADR-0009, punto 4). Se declara en el informe.
6. **Revocación (c6): «no se sirve, no se borra».** El código verifica el consentimiento en cada operación, antes de la
   caché y de las fuentes; el cliente de consentimiento de Perfilamiento no cachea y MS Identidad lee la base en cada
   consulta; si Identidad falla, el breaker responde «no otorgado». Por eso se espera el bloqueo en la primera consulta
   y el criterio se cumple sin V-11. Se mide con 50 clientes (consulta cada 1 s, hasta 300 s) y se comprueba aparte que
   sus claves siguen en Redis: el borrado de la caché al revocar queda para V-11.
7. **Clave de carga propia y temporal.** k6 usa `PERFILAMIENTO_API_KEY_CARGA` (origen `carga`), creada desde un Secret
   del clúster al empezar la sesión y borrada al terminar, para que la auditoría no atribuya la carga a Suscripción ni a
   Cotización. Usa el soporte de una clave por llamador de la enmienda HU24 (punto 7).
8. **Caché compartida: se aísla en la medición, se corrige aparte.** Durante la sesión, Cotización y Perfilamiento usan
   grupos de clientes disjuntos y la caché se vacía antes del escenario sin acierto. La corrección (prefijo por servicio
   o base distinta) es [solventa-backend#84](https://github.com/miso-proyecto-final-2026/solventa-backend/issues/84) y
   queda fuera de HA02; el informe lo registra como hallazgo.

## Consecuencias

- **Medición del 2026-11-15:** el veredicto por criterio y las decisiones 9 a 14 están más abajo.
  - El HPA por CPU de Perfilamiento basta: en la meseta no pasó de 2 réplicas ni de 69m de CPU.
  - El límite está en las dependencias: la consulta de consentimiento y la base.
- **Hallazgo:** la caché compartida entre Cotización y Perfilamiento (decisión 8) contradice el ADR-0001 y puede dar
  aciertos falsos y campos incompletos; queda abierta en solventa-backend#84.
- **Revocación:** el perfil deja de servirse de inmediato, pero las entradas de caché y respaldo viven hasta su TTL
  (hasta 24 h y 7 días); si la política de datos exige borrarlas, es trabajo de V-11.
- **Costo de la evidencia indirecta (c5):** si en el futuro se quiere una prueba directa, hará falta una métrica de
  llamadas por fuente en Perfilamiento.

## Medición en staging (2026-11-15)

### Montaje

**Cuenta y despliegue:** sesión en la cuenta del autor (`us-east-1`), desplegada desde ramas sin fusionar: backend
`4a4f898` e infra `647e4e9` (`feature/HU24-linaje-asesor`). Identidad usó la imagen `4a4f898-scripts`.

**Infraestructura:**
- EKS con 6 `t3.medium` fijos.
- RDS `db.t4g.micro` y Redis `cache.t4g.micro`.

**Carga:**
- k6 0.54.0 desde una `c5.large` en la VPC, contra el NLB interno.
- ~11.800 clientes sintéticos.
- Un escenario por corrida, de 16:42 a 19:44 UTC.

| Criterio de HA02 | Veredicto | Evidencia |
|---|---|---|
| c1 — p95 ≤ 400 ms y p99 ≤ 800 ms | **No cumple** | miss p95 112,1 / p99 195,7 ms y hit 36,7 / 102,3 ms cumplen. lenta_4a 529,3 / 3.968,2 ms: con todo sin acierto y Open Finance hasta 500 ms, el p95 no puede bajar de ~500 ms |
| c2 — ≥ 20.000/h sin saturación | **No cumple** | 18.121 respuestas 201 con consentimiento en la hora de meseta; 4 errores por saturación. Con los falsos bloqueos habrían sido 20.552 |
| c3 — la cotización conserva su meta | **No cumple** | p95 de la línea base 111,1 ms → meseta 456,7 ms (+345,6 ms). Corrida no válida para c3: 12,6 % de cotizaciones `aproximada` |
| c4 — degradación controlada | **No cumple** | lenta_4b con 100 % de 201 y 92,3 % degradadas, pero p95 724,4 ms (el timeout de la fuente, 0,7 s, supera los 400 ms de c1). caida_5a y caida_5b con 100 % marcadas y 42 y 40 respuestas 403 |
| c5 — bloqueo sin consentimiento | **No cumple** (comprobación estricta) | 1.358 de 1.358 clientes sin consentimiento con 403 y 0 perfiles. La base (4.028 intentos) también cuenta los falsos bloqueos |
| c6 — revocación ≤ 5 min | **Cumple** | 49 de 49 bloqueados en ≤ 0,168 s. Las claves siguen en Redis («no se sirve, no se borra») |
| c7 — evidencia | **Cumple** | k6, muestreo, eventos, base, Redis y avisos de los logs |

### Falsos bloqueos

**Definición:** 403 a clientes **con** consentimiento.

**Mecanismo:** Perfilamiento falla cerrado cuando la consulta a Identidad pasa de 300 ms o el breaker está abierto
(5 fallos, 15 s). Con el circuito abierto no escribe aviso.

**Cifras:**
- Total: 2.567 de 36.724 solicitudes con consentimiento (7,0 %).
- Meseta: 2.431, el 11,8 %, con solo 488 avisos `identidad_no_disponible`.
- lenta_4a 51, caida_5a 42, caida_5b 40; humo, miss y calentar, 1 cada uno.

Cotización tiene el mismo cliente y el mismo breaker: respondió 3.787 cotizaciones `aproximada` en la meseta.

### Causa raíz

**No es la CPU de Identidad.**
- Sus 5,5 vCPU y 6 réplicas fueron de la siembra (bcrypt de ~11.800 registros).
- En la meseta tuvo una mediana de 78m, 2 réplicas y 21 % del objetivo del HPA.

**Las paradas siguen un ciclo de ~3 min y afectan a todos los servicios a la vez.**
- Empiezan a las 18:04, unos 10 min después de empezar la carga combinada.
- En cada parada:
  - ~100 falsos bloqueos y ~140 cotizaciones aproximadas por minuto;
  - el p95 de Perfilamiento sube a 0,6–3,3 s y el de Cotización a 1–9,8 s;
  - entre paradas, los dos vuelven a ~110 ms.
- Latencias de segundos en dos servicios, muy por encima del timeout de 300 ms de Identidad, apuntan a una dependencia
  compartida.

**Sospecha sobre la RDS `db.t4g.micro`,** la única base de todos los servicios:
- En cada operación:
  - Identidad hace 3 viajes a la base;
  - Perfilamiento escribe un perfil, dos filas de linaje y los intentos;
  - Cotización escribe la cotización, su latencia y la trazabilidad.
- Cada pod abre hasta 15 conexiones, y la siembra ya agotó `max_connections`.

**Verificación con CloudWatch** (solo lectura, 16:30–19:50 UTC, después del destroy). Serie por minuto en
`solventa-experimentos/ha02/resultados/cloudwatch-rds-por-minuto.csv`:

| Ventana (UTC) | CPU máx. | Conexiones máx. | Créditos mín. | Latencia lectura / escritura máx. | Memoria libre mín. | Swap |
|---|---|---|---|---|---|---|
| Siembra (16:30–16:41) | 17,7 % | 68 | 7,8 | 1,0 / 4,8 ms | 55 MB | 14 → 41 MB |
| Línea base de Cotización y meseta (17:43–18:54) | 42,1 % | 59 | 4,0 | 4,0 / 3,3 ms | 54 MB | 43 → 230 MB |
| Después (18:55–19:50) | 19,9 % | 49 | 3,2 | 1,8 / 13,3 ms | 52 MB | 153–205 MB |

- **No se confirma que la RDS cause el ciclo de ~3 min.**
  - Los créditos de CPU bajaron hasta 3,2, pero nunca llegaron a 0.
  - Las conexiones de la meseta no pasaron de 59, por debajo de las 68 de la siembra, cuando sí se agotaron (el
    máximo por minuto puede ocultar picos de segundos).
  - La CPU no precede a las paradas: su correlación con los falsos bloqueos es −0,03 en el mismo minuto y 0,85 en el
    minuto siguiente. Los picos de 20–42 % son consecuencia de cada parada, no su causa.
  - El disco no muestra cola (`DiskQueueDepth` ≤ 0,13).
- **Sí se confirma presión de memoria.** Con 1 GB, la memoria libre estuvo entre 52 y 114 MB, y el swap creció de 43
  a 230 MB durante la meseta. Encaja con que la degradación empeore hacia el final de la hora, pero no prueba que
  cause cada parada.
- **La causa del ciclo sigue siendo una hipótesis.** Distinguir entre checkpoints, autovacuum o pausas de los pools
  de los servicios pide Performance Insights o los logs de PostgreSQL (`log_checkpoints`,
  `log_autovacuum_min_duration`), que no estaban activos.

**El hallazgo repite el de HA03** ([ADR-0015](0015-autoescalado-por-solicitudes-concurrentes.md)). Con la misma
`db.t4g.micro`, HA03 agotó las conexiones (153 y 89 % de CPU, 9.741 `ReadTimeout` de Identidad en Cotización), y con
`db.t4g.medium` el humo 2 no tuvo fallos (aproximadas de 27,4 % a 0,04 %). Staging volvió después a `db.t4g.micro`
(`staging.tfvars`), y la decisión 7 de ese ADR (presupuesto de dependencias) sigue abierta. HA02 vuelve a medir sobre
una base al límite de memoria y compartida por todos los servicios.

## Decisiones nuevas (propuestas, a partir de la medición)

9. **Caché corta del consentimiento, compatible con la revocación en ≤ 5 min.**
   - Perfilamiento y Cotización guardan en memoria el último `OTORGADO` de cada cliente, con un TTL de 60 s. Nunca
     guardan un «no otorgado» que venga de un fallo.
   - Revocar deja de surtir efecto, como máximo, un TTL después. Así se cumple c6 (300 s) con margen. La decisión 6
     cambia de «bloqueo en la primera consulta» a «bloqueo en ≤ TTL».
   - Si se quiere el efecto inmediato, el evento de revocación de Identidad (bus de eventos) invalida la entrada.
   - Reduce la carga sobre Identidad y la base en proporción a las consultas repetidas.
10. **Timeout de 300 ms y respaldo vigente.**
    - Se mantiene un timeout corto, para proteger el camino crítico.
    - Si la consulta vence o el breaker está abierto, se usa la última verificación `OTORGADO` guardada, aunque haya
      pasado el TTL de la decisión 9, mientras tenga menos de 5 minutos (*stale-if-error*). El límite de c6 se sigue
      cumpliendo.
    - Sin verificación previa, se sigue fallando cerrado.
    - Antes de cambiar el valor del timeout se mide el p99 interno de Identidad.
11. **El breaker deja rastro y no se confunde con un bloqueo.**
    - **Avisos:** registrar un aviso al abrir y al cerrar el circuito, y contar las llamadas rechazadas (métrica por
      servicio).
    - **Respuesta sin verificación posible:** si no se puede verificar el consentimiento y no hay respaldo, responder
      `503 CONSENTIMIENTO_NO_VERIFICABLE`, que se puede reintentar, en lugar del `403 CONSENTIMIENTO_NO_VIGENTE`.
    - **Auditoría:** dejar el intento en `intentos_bloqueados` solo con un 403 real. Así c5 vuelve a cuadrar con la base.
    - Sigue fallando cerrado: sin verificación no se perfila. Cotización informa la `aproximada` con su causa.
12. **Subir la clase de la RDS de staging a `db.t4g.medium`** (2 vCPU, 4 GB).
    - **Por qué:**
      - es la segunda medición de rendimiento (HA03 y HA02) que se hace sobre una `db.t4g.micro` al límite: en HA03
        por conexiones y en HA02 por memoria y swap;
      - con `db.t4g.medium`, HA03 ya midió el comportamiento esperado;
      - mientras la base esté al límite, las mediciones de HA02 no aíslan el comportamiento de los servicios.
    - **Cómo:** cambiar `db_instance_class` en `staging.tfvars` (solventa-infra), en un PR con el plan revisado, no
      solo para la sesión de medición como en HA03. El costo adicional se apaga con el destroy de cada sesión.
    - **Junto con ella:**
      - cada servicio acota su pool (por ejemplo, 5 conexiones sin desborde) o se usa RDS Proxy o PgBouncer, lo que
        cierra la decisión 7 del ADR-0015;
      - revisar `pool_pre_ping`, que agrega un viaje en cada solicitud;
      - activar `log_checkpoints` y `log_autovacuum_min_duration` (o Performance Insights) en la próxima medición,
        para confirmar o descartar la causa del ciclo de ~3 min.
    - Esta decisión no depende de confirmar la hipótesis del ciclo: se justifica por la presión de memoria medida y
      por el antecedente de HA03.
13. **Identidad: optimizar antes que escalar.**
    - La consulta de consentimiento no está limitada por CPU, sino por la base.
    - Propuesta: una sola consulta, consentimiento con existencia del cliente, sin el `get` previo; y que Identidad
      escale con sus llamadores (mínimo acorde o la misma señal), como propuso el ADR-0015.
    - Escalar réplicas sin cambiar la base no corrige las paradas.
14. **Metas inalcanzables con el diseño actual.**
    - **lenta_4a:** la fuente tarda hasta 500 ms y todas las consultas son sin acierto, así que c1 (p95 ≤ 400 ms) no se
      puede cumplir.
    - **lenta_4b:** el timeout de la fuente (0,7 s) supera la latencia de c1.
    - Antes de la próxima medición hay que decidir una de dos opciones:
      - alinear el timeout de la fuente con el presupuesto de c1;
      - dar a 4a y 4b una meta de latencia propia, de degradación, en el issue #43.

### Condición para la próxima medición

**Antes de medir:**
- Las decisiones 9 a 11 implementadas.
- La 12 aplicada en staging (`db.t4g.medium`, pools acotados y logs de checkpoints y autovacuum).
- La 14 resuelta en el issue.

**Para aceptar este ADR:**
- Repetir miss, hit, la meseta (con la línea base de Cotización), lenta_4a, caida_5a y la revocación.
- Cumplir c1–c6, con los falsos bloqueos por debajo del 0,1 % y 0 % de cotizaciones aproximadas en la meseta.
