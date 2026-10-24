# ADR-0013 — Cache-aside y circuit breaker con sondeo en segundo plano para la latencia de cotización

- **Estado:** Decisión 1 Aceptada · Decisión 2 Propuesta (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-24
- **Historia:** HA01 (arquitectura, Sprint 1) — [issue #42](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/42)
- **Depende de:** [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (la cotización es síncrona, con presupuesto de latencia de ≤ 250 ms p95). **Relacionado:** [ADR-0007](0007-trazabilidad-de-consultas-a-proveedores-externos.md) (el registro de consultas a proveedores no debe sumar latencia a la cotización).
- **Fuente de las cifras:** `solventa-experimentos/ha01-ronda2/resultados/VEREDICTO.md` e `informe-ronda2.json` (salida de `scripts/analizar.py` sobre el JSON crudo de k6) para la ronda 2; `solventa-experimentos/docs/resultados-finales-experimentacion.md` (§3) para la ronda 1.

## Contexto

La cotización embebida consulta a Open Finance en el camino de la solicitud. El documento de arquitectura fija dos ASR:

| ASR | Meta | Escenario |
|---|---|---|
| RQ-L1 | p95 ≤ 250 ms y p99 ≤ 500 ms extremo a extremo; hit de caché ≥ 60 % desde el minuto 3 | A — normal: Open Finance a 200–500 ms |
| RQ-L1 | p95 ≤ 300 ms (p99 sin meta) | B — degradado: Open Finance a 800–1.500 ms |
| RQ-D2 | 0 % de cotizaciones fallidas; en B, 100 % marcadas como degradadas | A y B |

El diseño responde con tres patrones en el MS Cotización: **cache-aside** sobre Redis (TTL de 300 s y copia de respaldo de larga duración), **timeout duro** de 0,7 s hacia el proveedor y **circuit breaker** (abre tras 5 fallos consecutivos) con **degradación controlada** al último perfil cacheado o a un valor por defecto.

### Ronda 1 (semana 7, AWS, generador fuera de la VPC)

| Métrica | A — normal | B — degradado |
|---|---|---|
| p95 cliente | 357,6 ms (no cumple) | 123,9 ms (cumple) |
| p99 cliente | 565,3 ms (no cumple) | 829,7 ms (sin meta) |
| p95 interno | 261 ms | — |
| Hit de caché | 94,4 % | 0 % (esperado tras limpieza) |
| Cotizaciones fallidas | 0 % | 0 % |

Veredicto: **parcialmente validada**. RQ-D2 y el escenario B se cumplieron; el escenario A no. La ronda dejó tres asuntos abiertos:

1. **Punto de medición.** La brecha cliente − interna en p95 era de ~96 ms. Con el generador fuera de la VPC, no se podía separar la red inherente al producto del costo de medir desde una máquina de desarrollo.
2. **Cola larga en B.** El p99 de 829,7 ms, frente a un p95 de 123,9 ms, apuntaba al sondeo half-open: cada 30 s el breaker pasaba a semiabierto **dentro de una solicitud de usuario**, que pagaba la llamada completa al proveedor caído. Era una hipótesis no verificada con trazas.
3. **Expiración por tandas.** Se propuso añadir dispersión (jitter) al TTL.

Por eso se repitió la medición (ronda 2) con dos cambios en `solventa-backend@116e410`:

- **Sondeo half-open en segundo plano:** las solicitudes de usuario solo pasan con el circuito cerrado; un sondeador independiente prueba la recuperación cada 30 s (`app/proveedores/circuit_breaker.py`).
- **Jitter de ±10–15 % en el TTL** (`app/proveedores/config.py`: `jitter_min = 0.10`, `jitter_max = 0.15`).

## Resultados de la ronda 2 (2026-10-18, Docker local)

Mismo perfil que la ronda 1: rampa de 500 a 5.000 solicitudes/min en 15 min y meseta de 10 min. 91.249 solicitudes por escenario. «Régimen» = desde el segundo 180 de la corrida.

| Métrica | A — normal | B — degradado |
|---|---|---|
| p50 / p95 / p99 cliente | 11,1 / **301,5** / **484,8** ms | 11,4 / **63,9** / **106,5** ms |
| p50 / p95 / p99 interno | 8,4 / 273,6 / 469,7 ms | 8,7 / 53,0 / 90,4 ms |
| p95 cliente en régimen | 267,8 ms | 59,2 ms |
| Brecha cliente − interna (p95 / p99) | 27,9 / 15,1 ms | 10,9 / 16,1 ms |
| Hit de caché en régimen | **91,8 %** (miss 8,2 %) | 0 % |
| Cotizaciones fallidas / HTTP fallidas | 0 % / 0 % | 0 % / 0 % |
| Cotizaciones degradadas | 0 % | **100 %** |
| Cotizaciones `aproximada` (validez) | 0 % | 0 % |

## Cumplimiento de los criterios de aceptación

| # | Criterio | Resultado de la ronda 2 | Veredicto |
|---|---|---|---|
| 1 | Medición en el cliente e interna, reportadas por separado | Cliente: k6 (`http_req_duration`). Interna: `metadata.latencia_interna_ms` de cada respuesta. Ambas se reportan por escenario, con la brecha entre ellas (tabla anterior). | **Cumple** |
| 2 | Escenario A: p95 ≤ 250 ms, p99 ≤ 500 ms, hit ≥ 60 % desde el minuto 3, 0 % fallidas | p95 301,5 ms (no cumple); p99 484,8 ms; hit en régimen 91,8 %; 0 % fallidas | **Parcial** — falla el p95 |
| 3 | Escenario B: p95 ≤ 300 ms, 0 % fallos, 100 % degradadas, p99 reportado | p95 63,9 ms; 0 % fallidas; 100 % degradadas; p99 106,5 ms | **Cumple** |
| 4 | Con trazas, confirmar o descartar que el p99 de B venía del sondeo half-open | El p99 bajó de 829,7 a 106,5 ms y desapareció el patrón de una solicitud lenta cada 30 s. Es consistente con la hipótesis, pero la confirmación es a nivel de cliente: **no hay trazas** (sin OTel). | **Parcial** |
| 5 | Veredicto por meta con dato y archivo de origen; si RQ-L1 no cumple, descomponer el exceso entre transporte y servicio | `VEREDICTO.md` da el veredicto por meta con su fuente (`informe-ronda2.json`). Descomposición del p95 de A: brecha cliente − interna de 27,9 ms (transporte) y p95 interno de 273,6 ms, ya por encima de la meta (servicio). El exceso del servicio lo explican los miss de caché (ver «Lectura»). | **Cumple** |
| 6 | Ambiente descrito: ubicación del generador, exposición del servicio y desviaciones | k6 0.54.0 en un contenedor de la misma red de Docker que el servicio; recursos, versiones y host documentados en `VEREDICTO.md` y en `escenario-X/ambiente.txt`. Desviaciones declaradas (ver abajo). | **Cumple** |
| 7 | Backends de trazas y métricas desplegados y tableros con datos; si no, declarar la desviación | No se desplegaron (sin OTel). La desviación está declarada; las métricas salen del propio k6 y de `docker stats` cada 5 s. | **Cumple** (desviación declarada) |
| 8 | Paquete con salida cruda del generador, muestreo, eventos y logs, y video | Por escenario: JSON crudo de k6 (`escenario-X.json.gz`), resumen y reporte HTML, `docker-stats.csv`, `redis-flush.txt`, logs del orquestador (`corrida-A.log`, `corrida-B.log`) y videos de 26,7 min (`videos/escenario-A.mp4`, `escenario-B.mp4`, también adjuntos en el issue #42). **Se perdió el log de consola de k6 de B** (`escenario-B/k6.log.PERDIDO.txt`); su resumen está completo. En el repo solo están los reportes HTML; el JSON crudo (~550 MB por escenario sin comprimir) queda fuera de git. | **Parcial** |

### Lectura

- **A, p95.** En régimen fallan en caché el 8,2 % de las solicitudes. Al ser más del 5 %, el p95 cae dentro de las solicitudes que pagan la latencia del simulador (200–500 ms). Con este perfil, cumplir los 250 ms exige un hit de caché por encima del 95 % o un proveedor más rápido. El punto de medición ya no es la causa: la brecha cliente − interna bajó de ~96 a 27,9 ms con el generador en la misma red.
- **A, TTL con jitter.** El p95 por minuto muestra oleadas cada ~5 min (TTL de 300 s), por ejemplo 22:29–22:31 y 22:35–22:36 UTC. El jitter las suaviza pero no las elimina: la caché se llena durante la rampa y sus claves expiran por tandas. Es una observación sobre la latencia por minuto, no verificada clave por clave.
- **B, cola p99.** Ver criterio 4.
- **Picos de CPU, causa no identificada.** Hubo un pico de ~10 s por escenario con el CPU de Cotización al 94–101 % de su límite: en A a las 22:40:27 (p95 del minuto: 534 ms) y en B entre 23:16:27 y 23:16:36 (160 solicitudes de más de 300 ms, máximo 686 ms). El primer minuto de B (p95 719 ms) es esperado: son las llamadas que agotan el timeout de 0,7 s antes de que se abra el breaker.

## Decisión

### Decisión 1 — Mantener cache-aside y circuit breaker con sondeo half-open en segundo plano (Aceptada)

1. La cotización consulta el perfil con **cache-aside** sobre Redis: clave `perfil:{cliente_id}:{fuente}`, TTL de 300 s con jitter de ±10–15 % y copia de respaldo de 86.400 s para la degradación.
2. La llamada a Open Finance tiene un **timeout duro de 0,7 s**.
3. El **circuit breaker** abre tras 5 fallos consecutivos. Con el circuito abierto, la cotización se resuelve desde el respaldo o el valor por defecto y se marca como degradada; nunca falla.
4. El **sondeo half-open corre en segundo plano** cada 30 s. Ninguna solicitud de usuario paga el costo de descubrir que el proveedor sigue caído.
5. RQ-L1 se mide **sobre la latencia que observa el cliente**, no sobre la interna (decisión de la ronda 1, que se ratifica).

**Por qué:** RQ-D2 se cumple en las dos rondas (0 % fallidas, 100 % degradadas en B), el escenario B cumple con holgura (p95 63,9 ms frente a 300 ms) y el cambio del sondeo bajó el p99 de B de 829,7 a 106,5 ms. El incumplimiento restante (p95 de A) no viene del patrón sino de la política de caché (Decisión 2).

### Decisión 2 — Ajustar la política de caché para bajar el miss a menos de 5 % (Propuesta)

En régimen, el miss de caché es de 8,2 %; para que el p95 de A salga de la latencia del proveedor, debe quedar por debajo de 5 % (hit > 95 %). Se evaluarán, en este orden y midiendo cada una por separado:

1. **Refresco anticipado** (*refresh-ahead*): renovar en segundo plano las claves próximas a expirar, para que la solicitud de usuario no encuentre el hueco.
2. **TTL más largo**, acotado por la frescura que acepte el negocio (ver «Riesgos»).
3. **Precalentamiento** de la caché al arranque o al terminar el despliegue.

**Condición para pasar a Aceptada:** una nueva medición del escenario A, con el mismo perfil de carga, que dé **p95 cliente ≤ 250 ms** sin romper las demás metas (p99 ≤ 500 ms, 0 % fallidas, hit ≥ 60 % desde el minuto 3). Hasta entonces, RQ-L1 en operación normal sigue **sin cumplirse**.

## Alternativas descartadas

1. **Sondeo half-open dentro de la solicitud de usuario** (diseño de la ronda 1). Descartada: es la causa probable del p99 de 829,7 ms en B; con el sondeo en segundo plano el p99 bajó a 106,5 ms.
2. **Medir RQ-L1 con la latencia interna.** Descartada en la ronda 1 y ratificada: el usuario real también paga el transporte, y medir dentro del servicio daría un cumplimiento ficticio.
3. **Atribuir el p95 de A al punto de medición.** Descartada con los datos de la ronda 2: con el generador en la misma red, la brecha cliente − interna es de 27,9 ms y el p95 interno (273,6 ms) ya supera la meta.
4. **Quedarse solo con el jitter del TTL para eliminar las oleadas.** Descartada como solución suficiente: el jitter de ±10–15 % suavizó las oleadas pero no las eliminó, y el miss en régimen sigue en 8,2 %.
5. **Estado del breaker compartido entre réplicas (p. ej. en Redis).** Descartada por ahora: añade una dependencia y un salto de red en el camino de cada solicitud, y la ronda 2 no la pudo evaluar (1 réplica). Queda como riesgo aceptado (ver abajo).

## Consecuencias

- **Positivas:** la cotización no falla aunque Open Finance se degrade; en degradado el p95 queda muy por debajo de la meta; la cola larga en B ya no depende del sondeo. Los parámetros (timeout, umbral, intervalo, TTL y jitter) se configuran por entorno sin tocar código.
- **Negativas:** RQ-L1 en operación normal **sigue sin cumplirse** (p95 301,5 ms); hay un proceso de sondeo por réplica que mantener; el primer minuto de una degradación sigue pagando timeouts de 0,7 s hasta que se abre el breaker (p95 de 719 ms en el primer minuto de B).
- **Seguimiento:** implementar y medir la Decisión 2; confirmar con trazas la causa de la cola de B cuando haya OTel; investigar los picos de CPU de ~10 s; repetir la medición en AWS con 2 o más réplicas.

## Riesgos

- **Breaker en memoria por réplica.** El estado del circuito vive en el proceso (`CircuitBreaker` guarda los fallos en memoria). Con N réplicas, cada una debe acumular sus propios 5 fallos antes de abrir, así que al comenzar una degradación hasta 5 × N solicitudes pagan el timeout, y las réplicas pueden tener estados distintos durante un rato. La ronda 2 corrió con 1 réplica (staging tiene mínimo 2 con HPA), de modo que este efecto **no está medido**.
- **Frescura de los datos.** Con el circuito abierto se sirve la copia de respaldo, que puede tener hasta 86.400 s; la cotización sale marcada como degradada. Si la Decisión 2 alarga el TTL o adopta precalentamiento, aumenta la edad máxima del perfil en operación normal. El límite aceptable de frescura **no está definido** en los documentos y debe fijarlo el negocio antes de elegir el TTL.
- **Picos de CPU sin causa identificada.** Uno por escenario, de ~10 s, al 94–101 % del límite. Si se repiten en producción, pueden empujar el p99.

## Supuestos y desviaciones

1. **Corrida local en Docker en lugar de AWS** (decisión del equipo). Cotización e Identidad tienen 1 CPU y 512Mi, como los límites de staging. Postgres y Redis corren sin límite de recursos, sin TLS y sin réplicas. Host: Windows 11, Docker Desktop 28.0.1, 20 CPU lógicas y ~7,6 GiB para Docker. Supuesto: la latencia interna local es representativa de la de staging; la brecha de transporte en AWS será mayor que los 27,9 ms locales.
2. **Una sola réplica** de cada servicio y sin autoescalado (staging tiene mínimo 2 con HPA).
3. **Sin OTel:** no hay trazas ni tableros. La latencia interna es `metadata.latencia_interna_ms` y el análisis de la cola de B se hace sobre las latencias del cliente.
4. **Consulta síncrona a Identidad**, que la ronda 1 no tenía. Va en el camino de cada solicitud, antes de la caché, y está incluida en la latencia interna. Medida aparte (83 solicitudes/s, misma red y mismos clientes): p50 5,2 ms, p95 6,5 ms y p99 11,3 ms, ≈ 60 % del p50 y ≈ 2 % del p95 de la latencia interna de A. Es una aproximación: son dos mediciones distintas.
5. **Datos sembrados:** 1.000 clientes sintéticos con consentimiento `OPEN_FINANCE_OPEN_DATA` (`clientes.json`). En la ronda 1 eran IDs sintéticos sin Identidad.
6. **Log de consola de k6 de B perdido.** El host se quedó sin memoria a las 23:30:20 UTC, ocho minutos después del fin de la carga de B (23:21:42). El JSON crudo cubre la corrida completa (22:56:43–23:21:42), la memoria de Cotización llegó como máximo a 88 MiB de 512 MiB y la presión no coincidió con ningún tramo de carga, por lo que los datos se consideran válidos. Las muestras de `docker-stats.csv` de B posteriores a las 23:22 UTC son de reposo.

## Coherencia con el documento de arquitectura

La Decisión 1 confirma los patrones del documento (cache-aside, circuit breaker, degradación controlada) y concreta el sondeo en segundo plano y el jitter del TTL. Los cambios a los modelos están registrados en `solventa-experimentos/docs/resultados-finales-experimentacion.md` (§3.6 y tabla de cambios): presupuesto de latencia por etapa, dependencia síncrona de Identidad (p95 ≈ 6,5 ms) y dispersión del TTL. La Decisión 2, si se acepta, añadiría la política de refresco de la caché a la vista funcional del flujo de cotización.

## Definición de terminado

- [ ] Artefacto versionado en el repositorio y enlazado desde el issue #42 (este ADR; se enlaza al fusionar el PR).
- [ ] Revisado por al menos 1 integrante distinto del autor.
- [x] Decisión, alternativas descartadas y consecuencias explícitas; supuestos declarados.
- [x] Medición: método, ambiente, datos crudos y desviaciones documentados (los datos crudos están en el paquete de evidencia, fuera de git por tamaño).
- [x] Documento de arquitectura actualizado donde el resultado cambia un modelo (`resultados-finales-experimentacion.md`). **El PDF del documento de arquitectura (`docs/arquitectura-8-semanas/` del hub) no se modificó.**

## Enlaces

- Issue de la historia: [proyecto-final#42](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/42)
- Scripts y resultados de la ronda 2: [solventa-experimentos#1](https://github.com/miso-proyecto-final-2026/solventa-experimentos/pull/1)
- Bitácora de la sesión de la ronda 2: [solventa-hub#22](https://github.com/miso-proyecto-final-2026/solventa-hub/pull/22)
