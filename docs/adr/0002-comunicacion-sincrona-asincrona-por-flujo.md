# ADR-0002 — Comunicación sincrónica vs. asincrónica por flujo

- **Estado:** Aceptado
- **Fecha:** 2026-10-10
- **Historia:** HA10 (arquitectura, Sprint 1) — [issue #51](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/51)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (HA09) — fija los 7 microservicios cuyos flujos se deciden aquí.
- **Fuente:** `docs/arquitectura-8-semanas/documento-arquitectura-refinado-y-detallado.pdf` del hub (sección 4.7 "Event-driven (pub/sub asíncrono con Kafka)", tabla épica→conector, flujos 1-3 del documento de arquitectura)

## Contexto

Los flujos que cruzan varios de los 7 microservicios (ADR-0001) pueden implementarse con llamadas síncronas encadenadas o con eventos asíncronos sobre Kafka (MSK). Usar eventos en todas partes añade complejidad operativa que un equipo de 4 personas no necesita pagar donde no aporta; usar siempre REST síncrono genera acoplamiento temporal entre servicios (si el receptor está caído, el emisor se bloquea). Esta historia decide, flujo por flujo, qué modo usar y lo registra como ADR, en lugar de dejarlo implícito en cada implementación.

## Decisión

Se decide **por flujo**, no de forma uniforme para todo el sistema:

| Flujo | Modo | Justificación |
|---|---|---|
| **Cotización** (E01) | Síncrono (REST + Redis cache-aside + Open Finance con circuit breaker) | El usuario espera la cotización en la misma interacción; tiene presupuesto de latencia explícito (HA01, ≤ 250 ms p95 extremo a extremo). No hay beneficio de desacople: no cruza fronteras de disponibilidad hacia un consumidor que pueda procesar "después". |
| **Verificación de identidad / KYC** (E05) | Síncrono (REST + Open Finance) | El cliente está en medio del flujo de registro (HU01→HU02) esperando el resultado para continuar; no es un flujo de larga duración. |
| **Consulta de consentimiento** (E05) | Síncrono (REST, MS Identidad) | Es una lectura que el cliente o el asesor consulta en el momento; no hay nada que desacoplar. |
| **Suscripción y cobro** (E04, saga orquestada) | **Asíncrono** (Kafka; cada paso de la saga publica un evento de resultado) | Flujo de varios pasos secuenciales (decisión, cobro vía MS Pagos, firma, emisión) que puede tardar y que no debe bloquear a MS Suscripción si MS Pagos está temporalmente degradado. La saga reemplaza una transacción distribuida clásica y respeta la autonomía de cada servicio (ADR-0001). |
| **Pago paramétrico** (E07, siniestro) | **Asíncrono** (Kafka: evento paramétrico de entrada; MS Siniestros → MS Pagos vía evento, con `idempotency_key`) | Es exactamente el caso que motiva este patrón en el documento de arquitectura: si MS Pagos no está disponible, MS Siniestros no puede quedar bloqueado esperando. |
| **Notificaciones** (resultado de suscripción, confirmación de pago) | **Asíncrono** (disparadas por el evento de resultado del flujo que notifican) | Son un efecto secundario de otro flujo (suscripción o pago), no algo que el cliente esté esperando de forma bloqueante en esa misma llamada. |
| **Actualización de pólizas** | **Asíncrono** (evento Kafka) | El documento de arquitectura la agrupa explícitamente junto a notificaciones y confirmaciones de pago como "flujos de larga duración" que se desacoplan por eventos. |

Nota: la comunicación de socios (E08, API versionada) y la lectura de la billetera de pólizas (E09) ya estaban definidas como síncronas en el documento de arquitectura; se mantienen así y no requieren decisión adicional en este ADR porque no aparecen en el alcance de la historia.

## Criterios para decidir síncrono vs. asíncrono

**Síncrono cuando:**
- El usuario (o el sistema que llama) espera la respuesta en la misma interacción para poder continuar.
- Existe un presupuesto de latencia explícito que la llamada debe cumplir (ej. cotización, HA01).
- La operación es una simple lectura o validación sin efectos secundarios de larga duración.

**Asíncrono cuando:**
- El flujo es de larga duración o tiene varios pasos secuenciales entre servicios distintos (saga).
- Se necesita absorber picos de carga sin que el emisor se bloquee esperando al receptor.
- El emisor debe seguir operando aunque el consumidor esté temporalmente no disponible (desacople ante falla).
- El resultado de un flujo dispara un efecto secundario en otro dominio (ej. una notificación) que no es parte de la respuesta que el usuario original está esperando.

## Consecuencias de los flujos asíncronos

Para suscripción/cobro, pago paramétrico, notificaciones y actualización de pólizas (todos sobre Kafka/MSK):

- **Garantía de entrega:** *at-least-once* (Kafka puede reentregar el mismo evento ante reintentos). No se usa *exactly-once* a nivel de infraestructura.
- **Idempotencia de consumidores:** obligatoria. Ya implementada para los pagos (suscripción y siniestro) mediante `idempotency_key` persistida y validada antes de procesar (ADR relacionado con HA08). **Supuesto a confirmar:** el documento de arquitectura no detalla el mecanismo de idempotencia del consumidor de notificaciones push; antes de implementarlo hay que definir una clave de idempotencia equivalente (ej. id del evento de origen) para evitar notificaciones duplicadas ante reentrega.
- **Orden:** Kafka garantiza orden solo dentro de una misma partición (particionamiento por dominio y topic); no hay garantía de orden global entre particiones distintas. Los flujos aquí decididos no dependen de orden entre dominios distintos, solo dentro de la saga de un mismo caso (que corre en su propio flujo de eventos).
- **Manejo de duplicados:** se resuelve con la idempotencia del consumidor (arriba), no con deduplicación en el broker.

## Cotización síncrona (criterio de aceptación 4)

Queda registrado explícitamente que la cotización es y seguirá siendo síncrona, con su presupuesto de latencia extremo a extremo de **≤ 250 ms p95** (HA01, RQ-L1). No se evalúa una variante asíncrona para este flujo: el usuario necesita el resultado en la misma interacción.

## Criterio de reversión

Si para un flujo declarado asíncrono el desacople **no produce una mejora observable de resiliencia** (por ejemplo, ante la caída temporal de un consumidor, el sistema no se comporta mejor que con una llamada síncrona equivalente), ese flujo se revierte a síncrono y la reversión se documenta como una actualización de este ADR, indicando qué se midió y por qué no se sostuvo la decisión original.

## Plan de validación (Sprint 3)

Se ejecutará un experimento sobre el flujo de **notificaciones push**, comparando las dos variantes (síncrona vía REST directo al servicio de notificaciones, y asíncrona vía evento Kafka) bajo las mismas condiciones:

- **Métricas:** latencia percibida por el cliente hasta recibir la notificación, complejidad operativa de cada variante (número de componentes a operar y depurar), y comportamiento ante fallo del consumidor de notificaciones (¿se pierden notificaciones o se reintentan correctamente?).
- **Criterio de éxito:** la variante asíncrona debe mostrar una mejora observable en al menos el comportamiento ante fallo del consumidor (no pierde notificaciones cuando este cae temporalmente) sin degradar la latencia percibida de forma perceptible para el usuario; si no, aplica el criterio de reversión de la sección anterior.
- **Responsable:** quien ejecute el experimento en el Sprint 3 revisa este plan antes de correrlo (criterio de aceptación de la historia).

## Coherencia con el documento de arquitectura y con HA09

Esta decisión no contradice el documento de arquitectura (tabla épica→conector, sección 4.7) ni el ADR-0001 (HA09): los flujos síncronos/asíncronos aquí decididos usan exactamente los conectores ya definidos para cada microservicio (REST síncrono para Cotización/Identidad/Perfilamiento; Kafka para Suscripción↔Pagos y Siniestros↔Pagos). No se detectaron diferencias que requirieran corregir alguno de los dos documentos.

## Consecuencias

- **Positivas:** los flujos que realmente necesitan desacople (saga de suscripción, pago paramétrico, notificaciones, actualización de pólizas) lo tienen; los que no lo necesitan (cotización, KYC, consentimiento) se mantienen simples de implementar y depurar.
- **Negativas:** los flujos asíncronos requieren que cada consumidor implemente idempotencia explícitamente (no es gratis); la idempotencia del consumidor de notificaciones queda como trabajo pendiente de diseño, no solo de implementación.
- **Seguimiento:** el experimento del Sprint 3 sobre notificaciones puede derivar en una reversión documentada si no muestra la mejora de resiliencia esperada.
