# ADR-0001 — Descomposición en microservicios y bounded contexts

- **Estado:** Aceptado
- **Fecha:** 2026-10-09
- **Historia:** HA09 (arquitectura, Sprint 1) — [issue #50](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/50)
- **Fuente:** `docs/arquitectura-8-semanas/documento-arquitectura-refinado-y-detallado.pdf` del hub (documento de arquitectura de las 8 semanas previas)

## Contexto

Solventa es una plataforma insurtech sobre Open Finance (cotización, suscripción y siniestro asistido) con expansión prevista a 3 países. El documento de arquitectura ya definió una capa de microservicios de dominio con siete servicios autónomos. Esta historia formaliza esa decisión como ADR, la verifica con un análisis de acoplamiento y la usa como insumo de arranque del Sprint 1: fija las fronteras sobre las que trabajan las demás historias (HU y HA).

## Decisión

Se adopta una descomposición en **siete microservicios**, cada uno dueño de su propio esquema de base de datos (PostgreSQL/RDS), sin relaciones de llave foránea entre esquemas de servicios distintos:

| Servicio | Responsabilidad | Épica/ASR |
|---|---|---|
| **MS Identidad** | Ciclo de vida del cliente: registro, autenticación biométrica, consentimiento Open Finance (otorgamiento/revocación), KYC. | E05 · RQ-S1 |
| **MS Cotización** | Recibe solicitudes de cotización, consulta el perfil del cliente (caché o Open Finance), aplica el motor de rating y emite la cotización con coberturas y prima. | E01 · HA01 · RQ-L1 |
| **MS Perfilamiento** | Enriquece el perfil del cliente con datos de Open Finance y Open Data para generar la oferta personalizada de vida hipotecario. | E06 · HA02 · RQ-E1 |
| **MS Suscripción** | Orquesta la saga de suscripción: decisión automática, cobro de prima, firma electrónica y emisión de póliza. | E04 · HA09 · RQ-D1 |
| **MS Pagos** | Procesa cobros de prima y pagos de siniestro; tokeniza el PAN (nunca se persiste en Solventa). | E04 · HA05 · RQ-S2 |
| **MS Siniestros** | Recibe eventos paramétricos, valida condiciones de disparo y ejecuta el pago automático con clave de idempotencia. | E07 · HA08 · RQ-D1 |
| **MS Socios** | Expone APIs versionadas para que socios distribuidores integren cotización y suscripción en sus propios flujos. | E08 · HA07 · RQ-I1 |

## Fronteras explícitas

Para cada servicio: entidades que posee (esquema propio) y qué NO le corresponde (se referencia por id o por evento, nunca por FK cruzada).

| Bounded context | Datos propios | Qué NO le corresponde |
|---|---|---|
| Identidad | `Cliente`, `Consentimiento` | No conoce cotizaciones, pólizas ni pagos; solo expone `cliente_id` por referencia. |
| Cotización | `Cotización`, `Cobertura`, `Perfil_Cacheado` (técnico), `Log_Latencia` (técnico) | No persiste datos de identidad ni de suscripción; referencia a `Cliente` solo por id. |
| Perfilamiento | `Perfil_Enriquecido`, `Oferta_Personalizada` | No decide la prima final de Cotización ni persiste datos de pólizas; su salida se consume vía caché (Redis) o API. |
| Suscripción | `Suscripción` (estado de saga), `Póliza` | No procesa el cobro en sí (delega a Pagos vía evento); referencia a `Cotización` y `Cliente` solo por id. |
| Pagos | `Transacción`, `Token_Tarjeta` | No conoce el detalle de la póliza ni del siniestro, solo su id de referencia; el PAN nunca se persiste (HA05, PCI-DSS). |
| Siniestros | `Evento_Paramétrico`, `Pago_Siniestro` | No ejecuta el pago (delega a Pagos vía evento Kafka); referencia a `Póliza` solo por id. |
| Socios | `Cliente_API`, `Contrato_API` | No posee datos de negocio (cotizaciones, pólizas); actúa como fachada hacia Cotización/Suscripción. |

## Reglas de dependencia

- Cada microservicio persiste y consulta únicamente su propio esquema en PostgreSQL. No se comparten llaves foráneas ni se hacen consultas cruzadas entre esquemas de bounded contexts distintos.
- Las relaciones entre contextos se resuelven **por identificador** (ej. `cotización_id` en la solicitud de suscripción) o **por evento** (Kafka/MSK, para flujos de larga duración: suscripción→pagos, pagos→suscripción, siniestros→pagos).
- La saga de suscripción (orquestada por MS Suscripción) reemplaza transacciones distribuidas y respeta la autonomía de cada servicio: un cambio de versión en un servicio (ej. nuevo producto en Cotización) no exige coordinar despliegues con los demás.

## Análisis de acoplamiento

Se evalúan tres cambios representativos sobre la descomposición elegida (7 servicios) y sobre una **candidata alternativa** de granularidad más gruesa (4 servicios, consolidando dominios con alta cercanía funcional): **Cliente** (Identidad + Perfilamiento), **Cotización**, **Contratación** (Suscripción + Pagos), **Operaciones** (Siniestros + Socios).

| Cambio representativo | Servicios afectados — 7 servicios (elegida) | Servicios afectados — 4 servicios (candidata) |
|---|---|---|
| **Nuevo ramo de seguro** (ej. auto): nuevas reglas de rating y nuevo flujo de emisión de póliza | Cotización, Suscripción → **2** | Cotización, Contratación → **2** |
| **Cambio de regla de rating** (ej. nuevo factor de riesgo en la prima) | Cotización → **1** | Cotización → **1** |
| **Cambio regulatorio** (ej. nueva exigencia de retención de datos KYC y reporte de transacciones a la Superintendencia) | Identidad, Pagos → **2** | Cliente, Contratación → **2** |

### Lectura del resultado

- En la descomposición elegida, los tres cambios afectan como máximo **2 servicios**, cumpliendo el criterio de éxito (≤ 2; revisión obligatoria si fueran ≥ 4).
- La candidata de 4 servicios da el mismo conteo de acoplamiento en estos tres cambios, por lo que el análisis de acoplamiento por sí solo **no decide** entre ambas particiones.
- El criterio que desempata a favor de los 7 servicios es la **autonomía operativa**: HA01 exige p95 de 250 ms en cotización y HA02 exige ~20.000 perfilamientos/hora sostenidos. Consolidar Cotización+Perfilamiento o Suscripción+Pagos en un mismo deployable acoplaría su escalado (HPA) y sus despliegues, contradiciendo la necesidad de escalar Cotización y Siniestros de forma más agresiva e independiente (ver vista de despliegue del documento de arquitectura).
- Costo asumido: complejidad operativa de siete servicios para un equipo de cuatro personas (documentado explícitamente, no se oculta el trade-off).

## Trazabilidad: HU del Sprint 1 por servicio

Mapeo derivado de la responsabilidad de cada servicio (sección anterior) y de los ejemplos dados en la historia (HU01–HU06 → Identidad; HU09–HU14, HU37 y HU38 → Cotización), extendido de forma consistente al resto de HU del tablero.

| Servicio | HU |
|---|---|
| **Identidad** | HU01, HU02, HU03, HU04, HU05, HU06, HU07, HU08 |
| **Cotización** | HU09, HU10, HU11, HU12, HU13, HU14, HU37, HU38 |
| **Perfilamiento** | HU23, HU24, HU25, HU26 |
| **Suscripción** | HU15, HU16, HU17, HU20, HU21, HU22, HU35, HU36, HU39 |
| **Pagos** | HU18, HU19, HU31 |
| **Siniestros** | HU27, HU28, HU29, HU30 |
| **Socios** | HU32, HU33, HU34 |

## Coherencia con el documento de arquitectura

Esta decisión no contradice el documento de arquitectura (`vision-de-arquitectura.pdf`, `documento-arquitectura-refinado-y-detallado.pdf`): los siete servicios, sus responsabilidades, el modelo de datos por bounded context y las reglas de dependencia se tomaron directamente de ese documento. No se detectaron diferencias que requirieran corrección.

## Consecuencias

- **Positivas:** cada servicio escala y despliega de forma independiente (requisito de HA01/HA02); los límites de datos son claros y verificables; el acoplamiento por cambio se mantiene ≤ 2 servicios en los escenarios evaluados.
- **Negativas:** mayor complejidad operativa (7 deployables, 7 esquemas, saga orquestada) para un equipo de 4 personas; la trazabilidad de un flujo de negocio completo (ej. cotización→suscripción→pago) requiere seguir eventos entre varios servicios en lugar de una sola transacción local.
- **Seguimiento:** si al avanzar el Sprint un cambio real afecta 4 o más servicios, se debe revisar esta frontera antes de continuar (criterio de éxito definido arriba).

## Alternativas descartadas

1. **Monolito modular** (un solo deployable con 7 módulos internos). Descartado: no permite escalado independiente de Cotización/Siniestros (HA01/HA02) y reintroduce acoplamiento de despliegue entre todos los dominios.
2. **Partición de 4 servicios** (Cliente, Cotización, Contratación, Operaciones). Descartada como decisión final, aunque se usó como candidata de comparación en el análisis de acoplamiento: da el mismo resultado de acoplamiento en los tres cambios evaluados, pero acopla el escalado de Identidad+Perfilamiento y de Suscripción+Pagos, contradiciendo las metas de rendimiento ya fijadas (HA01, HA02).
