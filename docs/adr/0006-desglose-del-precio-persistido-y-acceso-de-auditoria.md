# ADR-0006 — Desglose del precio persistido y acceso de auditoría

- **Estado:** Propuesto (el mecanismo de acceso es provisional hasta que exista la gestión de roles internos)
- **Fecha:** 2026-10-17
- **Historia:** HU14 (E01 · F02 Cálculo de precio, Sprint 1)
- **Depende de:** [ADR-0003](0003-motor-de-rating-reglas-versionadas-y-migraciones-alembic.md) (motor puro, migraciones Alembic) y [ADR-0005](0005-esquema-canonico-perfil-unificado.md) (fuente y fecha por campo).

## Contexto

Un auditor debe poder explicar y reproducir el precio de cada cotización emitida. El motor ya devolvía cuatro componentes que suman la prima, pero no se guardaban y no mostraban qué factor se aplicó ni de dónde venían los datos. La API de cotización no tiene autenticación y el servicio de Identidad no modela roles.

## Decisión

1. **Estructura:** un documento JSON versionado (`version_esquema`) en una tabla propia `cotizacion.desgloses_precio`, 1:1 con la cotización (clave primaria = `cotizacion_id`). Se descartó una tabla de componentes: el desglose es una unidad que se escribe y se lee completa, su forma cambia con las reglas (un producto nuevo trae factores nuevos) y una tabla por componente obligaría a una migración por cada cambio. El costo es consultar por contenido, que la auditoría no necesita. `version_esquema` permite evolucionar el formato.
2. **Contenido:** prima base; cada factor aplicado (`dato`, `riesgo_base`, `senal`) con su valor decimal; porcentaje del recargo conservador; los componentes; la versión de reglas; y una referencia al perfil usado (fuentes, `degradado`, `con_consentimiento`, `completitud`). Cada señal cita su `fuente` y `obtenido_en`. **No contiene** el identificador del cliente, los datos del producto ni el valor de las señales: solo factores y referencias. Los decimales viajan como texto para no perder exactitud.
3. **Invariante:** antes de persistir se verifica que la suma de los componentes sea la prima final **y** que el desglose reproduzca esa prima. Si falla, no se emite ni se guarda nada, se registra el error (`desglose inconsistente`, con producto y versión de reglas, sin datos del cliente) y se responde 500 `DESGLOSE_INCONSISTENTE`.
4. **Reproducibilidad:** `recalcular(desglose)` aplica la misma secuencia de redondeos del motor usando solo el desglose. Una prueba de propiedades la compara con el precio original para ambos productos y cualquier combinación de señales.
5. **Costo:** el desglose se inserta en la misma transacción que la cotización y sus coberturas (una sola ida a la base). Medido contra PostgreSQL 16 en contenedor, persistir el desglose agregó 0,28 ms al p95 de la escritura (1,37 → 1,66 ms).
6. **Inmutabilidad:** el repositorio solo expone inserción (dentro de `guardar`) y lectura; y en PostgreSQL un trigger `BEFORE UPDATE OR DELETE` rechaza cualquier modificación o borrado, incluso hecho a mano. Un recálculo es una cotización nueva con su propio desglose.
7. **Acceso (provisional):** `GET /v1/cotizaciones/{id}/desglose` exige una clave de servicio dedicada en `X-Service-Key`, tomada de `AUDITOR_API_KEYS` (lista separada por comas). Sin cabecera responde 401; con una clave no autorizada, 403; sin claves configuradas nadie accede (falla cerrado). La credencial se valida **antes** de buscar la cotización, así que no se revela si existe. La comparación es en tiempo constante. Es el patrón de clave de servicio que ya usa HU06 y da permisos mínimos: la clave solo abre este endpoint.
8. **Cotizaciones anteriores a HU14:** no tienen desglose. `GET /v1/cotizaciones/{id}` devuelve `componentes: null` y el endpoint de auditoría responde 404 `DESGLOSE_NO_DISPONIBLE`. No se reconstruye retroactivamente: sería inventar una explicación que no ocurrió.

## Consecuencias

- **Pendiente:** cuando exista la gestión de usuarios, roles y permisos internos (F26), el rol de auditor debe pasar a ser un rol real con sesión y trazabilidad de quién consultó. Una clave compartida identifica al servicio, no a la persona.
- **Pendiente (HU11):** la trazabilidad de datos consultados puede enlazarse por `cotizacion_id` con este desglose.
- **Gestión de la clave:** `AUDITOR_API_KEYS` debe ir en un secreto del ambiente (nunca en el repositorio) y rotarse; se admite más de una clave para rotar sin corte.
- **Despliegue:** la migración `0002` debe correr antes de desplegar la versión que escribe desgloses.
