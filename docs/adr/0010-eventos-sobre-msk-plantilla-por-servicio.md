# ADR-0010 — Eventos sobre MSK: SASL/IAM y plantilla de cliente por servicio

- **Estado:** Aceptado
- **Fecha:** 2026-10-23
- **Historia:** HS2 (infra, Sprint 2) — [issue #93](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/93)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (servicios autónomos), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (qué flujos son asíncronos) y [ADR-0009](0009-ms-perfilamiento-enriquecimiento-y-replica-de-proveedores.md) (se replica, no se comparte)
- **Fuente:** documento de arquitectura (conector Kafka) y documentación técnica §4 (sobre, topics y eventos)

## Contexto

El ADR-0002 deja asíncronos la saga de suscripción, el pago paramétrico y las notificaciones. Hace falta el bus (Amazon MSK) y una
forma común de usarlo desde Python que respete la autonomía de los servicios. HU15 y HU18 son las primeras que publican eventos.

## Decisión

1. **Autenticación SASL/IAM con EKS Pod Identity.** Cada servicio tiene un rol que solo puede publicar o consumir **sus** topics
   (los que declara en `eventos` de `services.yaml`), escribir en la cola de fallidos de lo que consume y usar los grupos de
   consumo que empiezan por su nombre. No hay usuarios ni claves que guardar ni rotar. Un rol aparte crea topics y no lee ni
   escribe eventos.
2. **Topics declarados y creados por la infraestructura**, nunca por accidente (`auto.create.topics.enable=false`): contrato
   de la documentación técnica §4.2, con clave de partición, y una cola de mensajes fallidos `<topic>.dlq` por topic.
3. **Plantilla de cliente copiada por servicio** (`docs/plantillas/eventos/`), no una librería compartida: una librería común
   obligaría a versionar y desplegar los servicios juntos (ADR-0001, ADR-0009). Se prueba en esta repo contra un Kafka real.
4. **Entrega al menos una vez e idempotencia en la base de datos:** el consumidor reserva `(grupo, event_id)` en la misma
   transacción del manejador. Una reentrega se omite y se cuenta; si el manejador falla, todo se deshace.
5. **Reintentos con retroceso exponencial** (base 1 s, tope 30 s, 5 intentos) y, agotados, envío a `<topic>.dlq` con el error y el
   origen. El offset solo se confirma cuando el mensaje terminó.
6. **Dos brokers `kafka.t3.small`** (uno por zona), replicación 2 con `min.insync.replicas=1`: en staging se prioriza el costo
   y seguir funcionando con un broker caído sobre la durabilidad estricta.

## Consecuencias

- **Positivas:** permisos mínimos verificables por servicio; sin secretos de Kafka; el contrato de eventos vive en un solo
  archivo versionado; los servicios no se acoplan por código.
- **Negativas:** cada copia de la plantilla puede divergir (se mitiga con sus pruebas, que viajan con ella); `min.insync.replicas=1`
  admite perder un evento si el broker líder cae antes de replicar; el desarrollo local solo cubre `PLAINTEXT`, la ruta IAM se
  prueba únicamente contra MSK (prueba de humo).
- **Abierta — doble escritura:** publicar después de confirmar la base de datos puede perder el evento si el proceso cae entre
  ambos pasos. Un patrón *outbox* lo resolvería; se evaluará en HU15 con la saga, que es el primer flujo donde importa.

## Alternativas descartadas

- **Librería compartida:** acopla versiones y despliegues (ADR-0001/0009).
- **SASL/SCRAM con usuarios y claves en Secrets Manager:** más secretos que rotar y permisos menos finos.
- **Outbox desde el principio:** costo y complejidad sin un flujo que lo exija todavía.
- **Crear topics automáticamente:** un error de nombre crearía un topic sin permisos, sin cola de fallidos y sin retención definida.
