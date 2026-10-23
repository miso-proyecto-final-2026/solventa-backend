# ADR-0012 — MS Suscripción: saga con decisión automática, reglas versionadas y cola de salida

- **Estado:** Aceptado
- **Fecha:** 2026-10-23
- **Historia:** HU15 (Sprint 2) — [issue #68](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/68)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (relación por identificador), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (la saga es asíncrona), [ADR-0003](0003-motor-de-rating-reglas-versionadas-y-migraciones-alembic.md) (reglas versionadas y migraciones) y [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (eventos sobre MSK)

## Contexto

La suscripción es una saga de cuatro pasos (decisión → cobro → firma → emisión). HU15 entrega el servicio, el estado y la
persistencia de la saga y el primer paso, la decisión automática. Los umbrales de decisión no están en los documentos: los
define el área de suscripción o actuaría.

## Decisión

1. **La decisión no bloquea la petición.** `POST /v1/suscripciones` guarda la saga en `EN_PROGRESO` / `DECISION`, responde 202 y decide
   enseguida en segundo plano; el cliente consulta con `GET`. Si el proceso cae antes de decidir, un barrido retoma las sagas sin
   decidir (las filas se toman con `FOR UPDATE SKIP LOCKED`, y decidir dos veces es inofensivo).
2. **Reglas en un archivo versionado** (`reglas/v1.yaml`) que solo lee `reglas.py`; `decision.py` es una función pura que no conoce
   HTTP, base de datos ni eventos (una prueba de arquitectura lo verifica). Cambiar un umbral toca un archivo. **Los valores de v1
   son provisionales** y están marcados así en el archivo y en la respuesta (`reglas_version`).
3. **Cuatro resultados:** `APROBADA` (la saga avanza al cobro), `RECHAZADA` (termina sin cobrar), `AJUSTADA` (se guarda el ajuste; la
   saga espera la aceptación explícita del cliente, que es de HU17) y `DERIVADA` (se registra una revisión asistida y la saga
   espera). Si faltan datos, no hay consentimiento o Perfilamiento no responde, se deriva: nunca se aprueba ni se rechaza a ciegas.
4. **Cola de salida (outbox).** El evento se guarda en `eventos_salida` en la misma transacción que el cambio de la saga y un relevo
   lo publica en MSK. Así no se pierde un evento si el proceso cae entre confirmar la base y publicar; los consumidores son
   idempotentes (ADR-0010), de modo que un duplicado no daña. El token del medio de pago viaja en el evento y se **redacta** al
   publicarlo; la saga tampoco lo conserva una vez que deja de hacer falta.
5. **Idempotencia entre réplicas por restricción de base de datos:** `(cliente_id, clave_idempotencia)` es única. Repetir la
   solicitud devuelve la misma saga; usar la clave con otra cotización es un conflicto.
6. **Una cotización se suscribe una sola vez.** No existe un estado «usada» en Cotización ni el ADR-0001 permite escribir en su
   esquema, así que la regla vive en Suscripción: índice único parcial sobre `cotizacion_id` para las sagas que no estén
   `RECHAZADA` o `COMPENSADA` (una saga caída libera la cotización).
7. **El cliente se autentica con su token.** Suscripción pregunta a Identidad (`GET /v1/sesion/actual`, con clave de servicio) a quién
   pertenece. Una cotización o suscripción ajena se responde como inexistente.
8. **Monto asegurado = la cobertura de mayor monto** de la cotización. Las demás coberturas (p. ej. incapacidad) forman parte de esa suma asegurada; sumarlas la contaría dos veces.

## Consecuencias

- Hay dos tablas más que mantener (cola de salida y evidencia de eventos) y un proceso de relevo por réplica.
- Los umbrales provisionales deben validarse con el área de suscripción antes de producción; el contrato no cambia al hacerlo.
- Los casos no cubiertos por las reglas aprobadas (uno o dos reportes negativos) se derivan a una persona por prudencia.
