# ADR-0016 — Tolerancia a la caída de una zona: réplicas repartidas con DoNotSchedule + Honor y hallazgos del simulacro

- **Estado:** Propuesta. La decisión de reparto se observó funcionando en staging; los criterios de RTO, disponibilidad y
  despliegue sin interrupción **no se cumplen** en la medición del 2026-10-25, y las propuestas 1 a 5 quedan pendientes.
- **Fecha:** 2026-10-25
- **Historia:** HA04 (arquitectura, Sprint 2) — [issue #45](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/45)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (servicios desplegados por separado, cada uno con sus réplicas). **Relacionado:** [ADR-0013](0013-cache-aside-circuit-breaker-latencia-cotizacion.md) (la caché es efímera y Cotización la usa con cache-aside) y [ADR-0015](0015-autoescalado-por-solicitudes-concurrentes.md) (HPA por concurrencia; el gauge inflado de este simulacro lo afecta).
- **Fuente de las cifras:** `informe-ha04.json`, `eventos.csv` y `sesion.log` de la sesión del 2026-10-25 (paquete de evidencia de `solventa-experimentos/ha03-ha04/resultados/`, fuera de git).

## Contexto

HA04 (RQ-D2, EC-D2.1 y EC-D2.2) pide que la plataforma tolere la caída de una zona de disponibilidad: RTO ≤ 10 min,
RPO ≤ 30 s, 99,97 % de disponibilidad, despliegue sin interrupción, réplicas repartidas entre zonas y caché efímera, con
informe y procedimiento repetible (criterios 1 a 8 del issue #45).

Staging corre en 2 zonas (us-east-1a y us-east-1b) con 6 nodos fijos `t3.medium` (3 por zona), RDS PostgreSQL Multi-AZ,
Redis con réplica en la otra zona y 2 réplicas mínimas por servicio. Antes de este cambio, la dispersión por zona del
módulo `service-workload` de `solventa-infra` era `ScheduleAnyway`: el planificador podía dejar las dos réplicas de un
servicio en la misma zona, y perder esa zona dejaba el servicio sin pods.

## Decisión

1. **Reparto obligatorio por zona:** `topologySpreadConstraints` con `topologyKey = topology.kubernetes.io/zone`,
   `maxSkew = 1` y `whenUnsatisfiable = DoNotSchedule` en todos los servicios
   ([solventa-infra#18](https://github.com/miso-proyecto-final-2026/solventa-infra/pull/18)).
2. **`nodeTaintsPolicy = Honor`** en esa restricción: los nodos acordonados o con taints no cuentan como dominio. Sin
   esto, al acordonar una zona entera el planificador la seguiría contando con 0 pods y dejaría los pods nuevos
   `Pending` por sesgo.
3. **Por nodo, `ScheduleAnyway`** (`topologyKey = kubernetes.io/hostname`): se prefiere repartir entre nodos, pero no
   bloquea la programación cuando hay más réplicas que nodos.
4. RDS y Redis toleran la zona con su failover gestionado (Multi-AZ y réplica con failover automático); no se cambian.

## Medición en staging (2026-10-25)

Tráfico constante de 10 cotizaciones/s durante 60 min desde una `c5.xlarge` en us-east-1a contra el NLB interno, con
`cliente_id` únicos para contar el RPO. Corrida `20261026T012808Z-ha04-60m` (01:28:08–02:28:08 UTC): 35.967 solicitudes,
34.992 confirmadas. Fallas inyectadas una a la vez (`eventos.csv`):

| Falla (inicio UTC) | Qué pasó |
|---|---|
| Failover de RDS (01:31:03) | Failover 01:31:14–01:31:54 (40 s). Suscripción reinició sus 2 pods por sonda (exit 137) y se recuperó en ~60 s. **Un pod de Cotización quedó colgado** (ver Hallazgos) |
| Failover de Redis (01:36:39) | `test-failover` 01:36:44 → completado 01:37:00 (~16 s). Sin errores de Redis en los logs de Cotización |
| Pérdida de us-east-1b (01:40:37) | `cordon` + `drain` de sus 3 nodos (drain hasta 01:42:20). Los pods se reprogramaron en 1a; quedaron 2 `Pending` por CPU (`3 Insufficient cpu, 3 node(s) were unschedulable`): la décima réplica de Cotización (inflada, ver Hallazgos) y una de Siniestros, que quedó 1/2. `uncordon` a las 01:51:12 |
| Intervención manual (01:51:10) | Se borró el pod colgado; los timeouts bajaron de ~25 por minuto a ~1 |
| Despliegue bajo carga (01:55:26) | `rollout restart` de Cotización con 10 réplicas: 26 s |

Criterios de aceptación del issue #45, con su numeración:

| Criterio de HA04 | Veredicto | Evidencia |
|---|---|---|
| 1 — Caída simulada: los recorridos críticos siguen operando en la zona restante | **Cumple, con salvedad** | Con us-east-1b acordonada y drenada, todos los servicios siguieron con pods en us-east-1a y la cotización siguió respondiendo (ventana de la falla: 95,8 % de disponibilidad). Salvedades: Siniestros quedó con 1 de 2 réplicas (`Pending` por CPU) y los errores de la ventana incluyen los del pod colgado |
| 2 — RTO ≤ 10 min, midiendo por separado pods (< 2 min), RDS (< 5 min) y Redis (< 1 min) | **No cumple** | Por componente: RDS 40 s (01:31:14–01:31:54) y Redis ~16 s (01:36:44–01:37:00) cumplen; el drenaje de la zona tardó 1 min 43 s (01:40:37–01:42:20), con 2 pods sin reprogramar por CPU hasta el `uncordon`. El servicio no se restableció solo: el pod de Cotización colgado tras el failover de RDS necesitó intervención manual, **20 min 01 s** (01:31:09 → 01:51:10). En la pérdida de zona, el analizador marca 633 s hasta el último error, contaminado por ese pod |
| 3 — RPO ≤ 30 s, ninguna transacción confirmada se pierde | **Cumple** | 34.993 filas frente a 34.992 confirmadas: 0 perdidas (la fila de más se guardó aunque el cliente recibió timeout) |
| 4 — Disponibilidad ≥ 99,97 % durante la prueba | **No cumple** | 97,29 % (34.992 / 35.967) |
| 5 — Despliegues sin interrupción (EC-D2.2) | **No demostrado** | `rollout restart` de Cotización bajo carga en 26 s; en su ventana, 99,87 % de disponibilidad y 19 s con error, pero había un timeout residual de ~1 por minuto desde antes del rollout, así que los errores no se pueden atribuir solo al despliegue |
| 6 — Distribución real de las réplicas entre zonas | **Cumple al programar, no tras la bajada** | `DoNotSchedule` + `Honor` aplicado: al desplegar, 2 réplicas por servicio en 1a/1b; en la meseta de HA03, 5 y 5. Tras la bajada del HPA de Cotización a 2, ambas quedaron en us-east-1a |
| 7 — Datos efímeros: perder el caché solo sube la latencia, sin errores | **Parcial** | Se probó el failover de Redis (la réplica conserva los datos), no la pérdida del caché: ~16 s sin errores de Redis en Cotización; la ventana del analizador (94,4 %, 78 s con error) no lo aísla del pod colgado. Evidencia indirecta en HA03: tras vaciar la caché, la fase base tuvo 0 % de fallidas con p95 de 492 ms |
| 8 — Informe con veredicto y procedimiento repetible | **Cumple** | `informe-ha04.json`, `eventos.csv`, eventos del clúster, logs y `describe` del pod colgado; procedimiento en `solventa-experimentos/ha03-ha04/RUNBOOK.md` (paso 7) |

`DoNotSchedule` + `Honor` se comportó como se esperaba: con la zona acordonada no hubo pods `Pending` por sesgo de
topología, solo por falta de CPU en la zona que quedó.

## Hallazgos y propuestas

1. **Pod colgado tras el failover de RDS sin que `/health` lo detecte.** El pod `266pq` de Cotización respondió su último
   201 a las 01:31:09 y quedó con 537 solicitudes atascadas, pero siguió `Ready` porque readiness y liveness solo
   consultan `/health`, que no toca la base. El NLB le siguió mandando tráfico (~25–40 timeouts por minuto) hasta que se
   borró a mano: 20 min 01 s. **Propuesta:** readiness que verifique la base (y la caché) además del proceso, para que el
   balanceador saque un pod que no puede atender.
2. **Sin timeouts de consulta ni de solicitud.** Las conexiones al primario anterior se quedaron esperando sin límite.
   **Propuesta:** `connect_timeout` y `statement_timeout` en el pool de SQLAlchemy, y un límite por solicitud en la app,
   para que una dependencia caída produzca errores rápidos en vez de solicitudes atascadas.
3. **Gauge de concurrencia inflado por solicitudes atascadas.** Las 537 solicitudes del pod colgado subieron el promedio
   a 55,5 frente al objetivo de 16, y el HPA llevó Cotización a 10 réplicas sin carga real; con una zona acordonada,
   esas réplicas de más no cupieron. **Propuesta:** acotar el gauge (o excluir del conteo las solicitudes que superan el
   timeout) para que no escale el HPA (ADR-0015, decisión 8).
4. **La bajada del HPA deja las réplicas en una sola zona.** `DoNotSchedule` solo actúa al programar; al reducir réplicas,
   Kubernetes no tiene en cuenta la zona. Tras bajar a 2, las dos réplicas de Cotización quedaron en us-east-1a: perder
   esa zona en ese momento dejaría el servicio sin pods hasta reprogramar. **Propuesta:** evaluar el descheduler con
   `RemovePodsViolatingTopologySpreadConstraint`, o un mínimo de réplicas que garantice al menos una por zona tras la
   bajada.
5. **Una zona sola no alcanza para las réplicas actuales.** Con us-east-1b acordonada, la CPU solicitada en 1a quedó al
   90–98 % y 2 pods no cupieron. **Propuesta:** dimensionar los nodos de cada zona para las réplicas mínimas de todos los
   servicios más un margen (o cluster-autoscaler), y medirlo en el siguiente simulacro.

Observación sin propuesta todavía: Suscripción reinicia sus 2 pods por sonda en cada failover de RDS (dos veces en la
sesión) y se recupera sola en ~60 s.

## Alternativas descartadas

- **`ScheduleAnyway` por zona** (lo que había): no garantiza el reparto; las pruebas de mutación de infra#18 lo
  detectan (`spread-zone-schedule-anyway`).
- **`nodeTaintsPolicy = Ignore`** (el valor por defecto): al acordonar una zona entera, la seguiría contando y dejaría
  los pods `Pending` por sesgo; también lo detecta una mutación (`spread-taints-ignored`).
- **Antiafinidad de pods obligatoria por zona:** con más réplicas que zonas impide programar; la dispersión con
  `maxSkew = 1` reparte sin ese límite.

## Consecuencias

- **Positivas:** con 2 o más réplicas, cada servicio tiene pods en ambas zonas al desplegar y al escalar; perder una
  zona no deja servicios sin pods por sesgo de topología. RPO 0 en el simulacro.
- **Negativas:** si la zona que queda no tiene CPU, los pods quedan `Pending` (no se programan en la misma zona para
  "hacer espacio"); el reparto no se mantiene tras la bajada del HPA.
- **Seguimiento:** propuestas 1 a 5; repetir el simulacro con ellas y con 90 min de tráfico (en esta sesión fueron 60 por
  el horario).

## Supuestos y desviaciones

1. Con 2 zonas, «perder una zona» se simula acordonando y drenando sus nodos; RDS y Redis se prueban con su failover
   gestionado, no apagando la zona.
2. La carga entra por el NLB interno; el borde público (CloudFront, WAF, API Gateway) queda fuera de la medición.
3. Desviaciones de la sesión, compartidas con HA03: RDS `db.t4g.medium` en vez de `db.t4g.micro` e Identidad con mínimo
   4 réplicas (ADR-0015, Supuestos 4 y 5); despliegue desde ramas sin fusionar.
4. Medición: k6 se cortó a los 59 min 59 s por el `executionTimeout` por defecto de SSM (3600 s); falta el último segundo
   y el resumen, el crudo está completo hasta ahí. Las ventanas por falla del analizador están contaminadas por el pod
   colgado (01:31–01:51), así que la recuperación de cada falla no es atribuible solo a ella.

## Coherencia con el documento de arquitectura

Concreta la táctica de redundancia entre zonas de la vista de despliegue (réplicas en ambas zonas, RDS Multi-AZ, Redis
con réplica). Agrega como tácticas pendientes la detección de fallas por readiness profunda y los timeouts en las
dependencias. El PDF del documento de arquitectura (`docs/arquitectura-8-semanas/` del hub) no se modifica.

## Definición de terminado

- [ ] Artefacto versionado en el repositorio y enlazado desde el issue #45.
- [ ] Revisado por al menos 1 integrante distinto del autor.
- [x] Decisión, alternativas descartadas y consecuencias explícitas; supuestos declarados.
- [x] Simulacro en staging registrado en este ADR (2026-10-25).

## Enlaces

- Issue de la historia: [proyecto-final#45](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/45)
- Dispersión por zona: [solventa-infra#18](https://github.com/miso-proyecto-final-2026/solventa-infra/pull/18)
- Autoescalado (HA03): [ADR-0015](0015-autoescalado-por-solicitudes-concurrentes.md)
