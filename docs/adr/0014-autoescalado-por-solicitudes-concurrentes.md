# ADR-0014 — Autoescalado de Cotización por solicitudes concurrentes (Prometheus Adapter + HPA nativo)

- **Estado:** Propuesta (pasa a Aceptada con la medición de HA03 en staging; ver «Condición para aceptar»)
- **Fecha:** 2026-10-25
- **Historia:** HA03 (arquitectura, Sprint 2) — [issue #44](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/44), criterio 4 (métrica del HPA)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (Cotización escala de forma independiente y más agresiva que el resto). **Relacionado:** [ADR-0013](0013-cache-aside-circuit-breaker-latencia-cotizacion.md) (el caché es lo que desacopla la CPU del volumen).
- **Fuente de las cifras:** `solventa-experimentos/docs/resultados-finales-experimentacion.md` (§5, hallazgo transversal de HA02) para la relación CPU/volumen; ADR-0013 (ronda 2 de HA01) para las latencias internas.

## Contexto

HA03 exige que MS Cotización pase de 500 a 50.000 cotizaciones/min (≈ 833 solicitudes/s) y escale en ≤ 60 s sin degradar RQ-L1 ni producir errores por saturación. El criterio 4 pide que el HPA escale por una métrica que siga a la carga; la documentación técnica propone `http_concurrent_requests` con objetivo de 50 por réplica, mínimo 2 y máximo 10 réplicas, estabilización de 30 s al subir y 300 s al bajar, todo marcado «a confirmar con los resultados».

Hoy el HPA de staging (`solventa-infra`, `modules/service-workload`) escala solo por CPU, y el hallazgo transversal de HA02 muestra que esa señal no sirve para Cotización:

- Con caché, cada solicitud/s costaba 14,6 m de CPU a 5,6 solicitudes/s, pero solo 1,8 m a 166 solicitudes/s.
- El HPA por CPU **no escaló nunca**, ni a 30 veces la carga del ASR; habría hecho falta del orden de 380 solicitudes/s para llegar al 70 % de CPU.
- Conclusión registrada en el documento: la métrica debe ser el número de solicitudes concurrentes o en cola, no la CPU.

Además, el servicio no exponía ninguna métrica propia (`/metrics`), y el clúster solo tiene metrics-server (CPU y memoria).

## Decisión

1. **Señal.** Cotización expone el gauge `http_requests_in_flight` (solicitudes HTTP que el proceso está atendiendo) en `/metrics`, en formato de Prometheus (`app/metricas.py`):
   - Lo mantiene un middleware ASGI, el más externo de la cadena, que suma al entrar y resta en un `finally`: la cuenta vuelve a 0 aunque la solicitud falle o el cliente se desconecte.
   - No cuentan `/metrics` ni `/health` (el scrape y los sondeos no son carga de usuario).
   - Sin etiquetas: una sola serie por pod, cardinalidad mínima.
   - Registro propio de `prometheus_client`; `/metrics` queda fuera del contrato OpenAPI.
   - **Alcance de `/metrics` (aceptado):** no se publica por el borde (el API Gateway solo enruta los prefijos públicos
     de cada servicio y `solventa-infra` lo impide con una validación), pero **es legible dentro de la VPC**: comparte
     puerto con la app, así que llega por el NodePort del NLB interno y por el Service del clúster. Se acepta porque es
     un único gauge, sin etiquetas ni datos de clientes. Servirlo en un puerto aparte, que ningún Service ni NodePort
     expone (y, con NetworkPolicy, solo Prometheus), queda como mejora futura.
   - Un `Gauge` en memoria basta porque Cotización corre con un solo proceso de uvicorn por pod. Si algún día se usan varios workers, hay que pasar al modo multiproceso de `prometheus_client`.
2. **Transporte (alternativa A).** Un Prometheus mínimo en el clúster (sin Grafana, Alertmanager ni node-exporter; retención corta en `emptyDir`) recoge `/metrics` de los pods anotados, y **Prometheus Adapter** publica la métrica en la API `custom.metrics.k8s.io` con una regla suavizada: `avg_over_time(http_requests_in_flight[30s])` por pod.
3. **HPA nativo bajo Terraform** con dos métricas:
   - Tipo `Pods`, `http_requests_in_flight`, objetivo `AverageValue` configurable por servicio en `services.yaml`.
   - CPU como **respaldo**. El HPA toma la recomendación más alta, así que la CPU solo manda si la concurrencia no la refleja, y si Prometheus cae el HPA sigue escalando por CPU.
4. **Política de subida configurable por servicio** en `services.yaml`, abierta solo para Cotización. La política actual (2 pods cada 60 s) haría que 2 → 10 réplicas tardara unos 4 min aunque la métrica fuera la correcta, incompatible con el criterio 1 (≤ 60 s). Propuesta inicial: hasta duplicar cada 15 s. El resto de los servicios conserva la política actual.
5. **Calibración del objetivo.** El objetivo de 50 por réplica **no se adopta**. Por la ley de Little, las solicitudes en curso son tasa × tiempo en el servicio: a ≈ 833 solicitudes/s, con el p50 interno de 8,4 ms y el p95 de 273,6 ms de la ronda 2 de HA01, el total del clúster es del orden de decenas, no de cientos, y un objetivo de 50 por réplica no dispararía nunca. El objetivo queda en **unidades por réplica**. Es una estimación, no una medición: se fija con la prueba en kind (la cadena) y con el humo de la sesión en AWS (el valor), y el valor final se registra aquí antes de pasar a Aceptada.

La implementación va en dos PRs: este (señal + ADR) y el de `solventa-infra` (Prometheus, adaptador, contrato de `services.yaml`, HPA y política de subida).

## Alternativas descartadas

1. **Mantener el HPA por CPU** (planeado provisionalmente en la sesión como desviación del criterio 4). Descartada: HA02 mostró que no escala con caché, y Stiven aprobó cambiar a solicitudes concurrentes.
2. **B — Prometheus + KEDA (trigger de Prometheus).** Usa la misma señal y su `ScaledObject` es más legible y trae `fallback`, pero KEDA crea su propio HPA (el módulo tendría que dejar de generarlo para Cotización y producir un CRD) y agrega unos 3 pods. Sobre todo, sus finalizadores y webhooks pueden trabar el `terraform destroy` si el operador se borra antes que los `ScaledObject`, un riesgo real en un staging que se crea y se destruye en cada sesión. Queda como opción si más adelante se quiere escalar a cero o por métricas externas (p. ej., el rezago de Kafka en Siniestros).
3. **C — Add-on HTTP de KEDA.** No requiere cambios en la app, pero mete un interceptor (proxy) entre el NLB y el pod: un salto de red más en cada solicitud, que pega directo en el p95 de RQ-L1. Además, el interceptor también tiene que escalar con la carga y es un componente menos probado que el núcleo de KEDA.
4. **Objetivo de 50 por réplica** de la documentación técnica. Descartado por la estimación de la ley de Little (Decisión 5).
5. **Señal instantánea sin suavizar.** Descartada: con valores de unidades por pod, el gauge instantáneo es ruidoso y provocaría oscilaciones; el promedio de 30 s lo amortigua a costa de unos segundos de retraso.

## Consecuencias

- **Positivas:**
  - El HPA sigue a la carga real aunque el caché mantenga baja la CPU.
  - Nada nuevo en el camino de la solicitud más allá de sumar y restar un contador en memoria.
  - El HPA sigue en Terraform con sus pruebas y mutaciones, y el destroy no deja finalizadores.
  - La CPU queda como red de seguridad si Prometheus o el adaptador fallan.
- **Negativas:**
  - Dos componentes más en el clúster (Prometheus y el adaptador) que consumen recursos de los nodos fijos. Estimación previa, sin medir: del orden de 150 m de CPU y 400 Mi de memoria entre los dos; se mide en kind.
  - La configuración del adaptador es poco legible, aunque para una métrica son pocas líneas.
  - La cadena scrape → promedio de 30 s → ciclo del HPA agrega retraso a la reacción, y consume parte del presupuesto de 60 s del criterio 1.
- **Seguimiento:** puerto aparte para `/metrics` (mejora futura, ver Decisión 1); probar la cadena en kind; calibrar el objetivo en el humo de AWS; registrar en este ADR el objetivo final, el intervalo de scrape y la política de subida usados en la medición.

## Riesgos

- **Retraso de la señal.** El tiempo de reacción suma el intervalo de scrape, la ventana de 30 s del promedio, el periodo del HPA (15 s por defecto en Kubernetes) y el arranque del pod hasta estar listo. Si no cabe en 60 s, hay que acortar la ventana o el scrape antes que relajar el criterio.
- **Nodos fijos.** No hay cluster-autoscaler (decisión D6 de la sesión de HA03: `node_count = 6`). Si las 10 réplicas no caben, quedan `Pending` y el escalado se ve limitado por capacidad, no por la métrica; se declara como desviación.
- **Prometheus sin persistencia.** Si su pod se reinicia, se pierde la historia corta y la métrica no está disponible durante un rato; el HPA marca `ScalingActive=False` para esa métrica y sigue con la CPU.
- **`/metrics` legible dentro de la VPC.** Cualquier equipo de la VPC que llegue al NodePort, o cualquier pod del
  clúster, puede leer el gauge. Expone solo cuántas solicitudes atiende el pod en ese momento; riesgo aceptado (ver
  Decisión 1).
- **Un solo worker por pod.** Si se cambia el modelo de procesos de uvicorn sin pasar a modo multiproceso, la métrica solo reflejaría un proceso.

## Supuestos y desviaciones

1. La estimación de concurrencia usa latencias de la ronda 2 de HA01 (Docker local, 1 réplica), no de staging con varias réplicas.
2. La señal cuenta solicitudes **en curso dentro del proceso**: no ve las que esperan en la cola del socket o del balanceador antes de que uvicorn las acepte. Con un solo proceso asíncrono, la espera ocurre mayormente dentro del event loop y sí se cuenta.

## Condición para aceptar

Una corrida de HA03 en staging con este diseño que muestre:
- que el HPA escala por `http_requests_in_flight` (la CPU no es la métrica que dispara);
- que cumple los criterios 1 y 4 de HA03.

Se registran el objetivo, la política y las cifras medidas.

## Coherencia con el documento de arquitectura

Concreta el ajuste de parametrización registrado en `resultados-finales-experimentacion.md` (§5): la métrica del HPA de Cotización pasa de CPU a solicitudes concurrentes, y se recalibra el objetivo propuesto de 50 por réplica. La vista de despliegue gana dos componentes de observabilidad en el clúster (Prometheus y Prometheus Adapter). El PDF del documento de arquitectura (`docs/arquitectura-8-semanas/` del hub) no se modifica en este PR.

## Definición de terminado

- [ ] Artefacto versionado en el repositorio y enlazado desde el issue #44.
- [ ] Revisado por al menos 1 integrante distinto del autor.
- [x] Decisión, alternativas descartadas y consecuencias explícitas; supuestos declarados.
- [ ] Medición en staging (criterios 1 y 4 de HA03) registrada en este ADR.

## Enlaces

- Issue de la historia: [proyecto-final#44](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/44)
- PR de infraestructura (Prometheus, adaptador y HPA): pendiente, se enlaza al crearlo.
- Dispersión por zona para HA04 (mismo módulo): [solventa-infra#18](https://github.com/miso-proyecto-final-2026/solventa-infra/pull/18)
