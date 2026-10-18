# ADR-0007 — Trazabilidad de consultas a proveedores externos

- **Estado:** Propuesto (retención y durabilidad de la cola pendientes de acordar con el equipo)
- **Fecha:** 2026-10-18
- **Historia:** HU11 (E01 · F01 Integración con sistemas de datos externos, Sprint 1)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin llaves foráneas entre contextos), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md), [ADR-0005](0005-esquema-canonico-perfil-unificado.md) y [ADR-0006](0006-desglose-del-precio-persistido-y-acceso-de-auditoria.md) (acceso de auditor).

## Contexto

Hay que poder explicar de dónde salió la información usada en una cotización: qué proveedor se consultó, cuándo, con qué consentimiento y con qué resultado, incluidas las omisiones por falta de consentimiento. El registro no puede aumentar la latencia de la cotización (HA01) ni hacerla fallar, y no puede guardar respuestas de proveedores ni datos personales.

## Decisión

1. **Una huella por proveedor y consulta**, emitida desde `ServicioPerfil`, el único punto por el que pasan todas las consultas (también las de caché, las degradadas y las omitidas). Así la cobertura del 100 % no depende de que cada llamador se acuerde de registrar.
2. **Contenido:** `cliente_id`, proveedor, categorías de datos consultadas, fecha y hora, `consentimiento_id`, resultado (`OK`, `TIMEOUT`, `DEGRADADO`, `SIN_CONSENTIMIENTO`), latencia, `hit_cache`, `cotizacion_id` y correlación. **Nunca** el payload ni valores del cliente. Las categorías son un supuesto de prototipo, alineado con los campos canónicos de cada fuente.
3. **Resultado:** `TIMEOUT` si el proveedor no respondió a tiempo (aunque luego se sirviera el respaldo); `DEGRADADO` ante cualquier otra falla, respuesta vacía o circuito abierto; `SIN_CONSENTIMIENTO` registra una omisión por proveedor con latencia 0; `OK` incluye los aciertos de caché vigentes.
4. **`consentimiento_id` verificado:** Identidad añade `id` (opcional, aditivo) a la respuesta de consulta de consentimiento y Cotización lo lee en `verificar()`. Si Identidad no lo informa, queda `null`. El campo admite 64 caracteres y se recorta: un valor externo no debe poder impedir la escritura (lo descubrió la prueba contra PostgreSQL con el stub, cuyo id mide 41).
5. **Contexto sin cambiar firmas:** `contextvars` (`app/contexto.py`) llevan la correlación y la cotización en curso. Un middleware ASGI toma `X-Correlation-Id` si es seguro (`[A-Za-z0-9._:-]{1,64}`) o genera uno, y lo devuelve en la respuesta. `cotizar` fija el `cotizacion_id` **antes** de consultar al proveedor; `GET /perfiles` registra sin cotización.
6. **Escritura fuera del camino crítico:** `ServicioPerfil` solo encola (`put_nowait`, microsegundos, nunca lanza). Un trabajador de fondo escribe por lotes con reintentos de espera creciente; agotados, escribe fila a fila para aislar un registro inválido; lo que aun así falla se pierde con un log `ALERTA` y se cuenta (`perdidos`). Si la cola se llena se descarta con alerta (`descartados`). Al apagar, `detener()` escribe lo pendiente.
7. **Tabla `cotizacion.trazabilidad_consultas`**, solo inserción (el repositorio solo inserta y lee, y un trigger de PostgreSQL rechaza UPDATE y DELETE), con índices por `cotizacion_id` (con la fecha, para el orden temporal) y `cliente_id`. **Sin llave foránea** a `cotizaciones`: la escritura es asíncrona y puede llegar antes, o sin cotización.
8. **Consulta:** `GET /v1/cotizaciones/{id}/consultas-externas`, en orden temporal, con la credencial de auditor de ADR-0006. Es eventualmente consistente: un registro puede tardar un instante en aparecer.

## Consecuencias

- **Límite conocido:** la cola vive en memoria; si el proceso muere con registros pendientes se pierden. Para una auditoría estricta hace falta una cola durable (Kafka, ADR-0002). Se deja como pendiente.
- **Alertas:** hoy son logs de nivel ERROR con el prefijo `ALERTA` y contadores en el escritor; falta conectarlos a la monitorización cuando exista.
- **Retención:** no se implementa; el periodo se define con el equipo (supuesto: la política de datos del proyecto). La tabla crecerá sin límite hasta entonces.
- **Escala:** cada cotización escribe hasta 2 filas (una por proveedor); a ~20.000 perfilamientos/hora (HA02) son ~40.000 filas/hora.
- **Despliegue:** la migración `0003` debe correr antes de la versión que escribe la huella. Identidad debe desplegarse con el campo `id` para que `consentimiento_id` no sea nulo.
- **Pendiente:** el `openapi.json` de Identidad ya estaba desactualizado (le faltan las verificaciones de HU02); no se regeneró aquí para no mezclar cambios.
