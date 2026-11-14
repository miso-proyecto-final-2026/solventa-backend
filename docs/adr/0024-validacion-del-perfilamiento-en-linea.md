# ADR-0024 — Validación del perfilamiento en línea (HA02)

- **Estado:** Propuesta. Fija cómo se valida HA02 y qué se acepta como evidencia; los resultados y las decisiones que se
  deriven se agregan después de la sesión en staging, como en el ADR-0015.
- **Fecha:** 2026-11-14
- **Historia:** HA02 (arquitectura, Sprint 3) — [issue #43](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/43)
- **Depende de:** [ADR-0009](0009-ms-perfilamiento-enriquecimiento-y-replica-de-proveedores.md) (MS Perfilamiento:
  consentimiento en cada operación, caché con TTL por tipo de dato, linaje y enmienda HU24). **Relacionado:**
  [ADR-0013](0013-cache-aside-circuit-breaker-latencia-cotizacion.md) (caché, breaker y meta de la cotización en línea)
  y [ADR-0015](0015-autoescalado-por-solicitudes-concurrentes.md) (método de medición sobre staging).
- **Experimento:** `solventa-experimentos/ha02/` (scripts, `RUNBOOK.md` y, al cerrar, el informe con veredicto).

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

- **Pendiente de la medición:** el veredicto por criterio y las decisiones que se deriven (por ejemplo, si el HPA por
  CPU de Perfilamiento basta a 20.000/h o hace falta una métrica de concurrencia como en el ADR-0015). Este ADR pasa a
  Aceptado o se enmienda con esos resultados.
- **Hallazgo:** la caché compartida entre Cotización y Perfilamiento (decisión 8) contradice el ADR-0001 y puede dar
  aciertos falsos y campos incompletos; queda abierta en solventa-backend#84.
- **Revocación:** el perfil deja de servirse de inmediato, pero las entradas de caché y respaldo viven hasta su TTL
  (hasta 24 h y 7 días); si la política de datos exige borrarlas, es trabajo de V-11.
- **Costo de la evidencia indirecta (c5):** si en el futuro se quiere una prueba directa, hará falta una métrica de
  llamadas por fuente en Perfilamiento.
