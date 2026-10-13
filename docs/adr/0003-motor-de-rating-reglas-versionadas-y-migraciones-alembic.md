# ADR-0003 — Motor de rating con reglas versionadas y migraciones con Alembic

- **Estado:** Aceptado
- **Fecha:** 2026-10-12
- **Historia:** HU12 (E01 · F02 Cálculo de precio, Sprint 1)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (esquema propio por contexto, sin FK cruzadas) y [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (cotización síncrona).

## Contexto

El motor de rating lo comparten los canales móvil, web y API de socios. Debe cumplir el ASR de modificabilidad (RQ-M1: cambiar una fórmula o agregar un ramo afecta un solo componente, sin tocar la lógica) y el presupuesto de latencia interna de ≤ 150 ms p95 (HA01, RQ-L1). El hub no define tarifas, fórmulas ni reglas de redondeo, y la documentación técnica menciona Flyway, una herramienta de Java, para versionar el esquema.

## Decisión

1. **Núcleo funcional puro.** `calcular_prima(producto, datos, perfil, reglas)` no lee reloj, base de datos, caché ni red. Garantiza el determinismo y permite pruebas de propiedades. Los efectos (perfil, persistencia, medición) viven en la fachada `ServicioCotizacion`.
2. **Reglas dirigidas por datos.** Tarifas, factores, rangos de validación, coberturas y recargos están en `app/cotizacion/reglas/<version>.yaml`. Se validan al cargar (tablas contiguas y completas, factores coherentes con el sentido del riesgo) y la aplicación **no arranca** si son inválidas. La versión activa se elige con `REGLAS_VERSION` y cada cotización guarda la `version_reglas` aplicada. Agregar un producto es agregar un bloque al YAML, sin tocar el motor.
3. **Tres resultados de precio.** *Normal* (con señales), *aproximada* (sin consentimiento: factor base del producto y aviso de que el precio puede cambiar al comprar) y *degradada* (falla del proveedor: se ignoran las señales y se aplica el recargo conservador configurable). Una cotización degradada se emite igual (HTTP 201 con `degradado = true`): no se usa 503, para mantener 0 % de cotizaciones fallidas.
4. **Moneda y redondeo.** COP, enteros (sin decimales), `ROUND_HALF_UP` en cada paso. Los componentes del desglose se obtienen por diferencia entre pasos, por lo que su suma es exactamente la prima anual. La prima anual es la principal; la mensual es informativa (anual / 12 redondeada). En base de datos se guardan como `NUMERIC(15,2)`.
5. **Migraciones con Alembic (no Flyway).** El backend es Python con SQLAlchemy; Alembic es la herramienta nativa, mientras que Flyway añadiría una herramienta de Java al pipeline. Cada microservicio versiona su propio esquema: la migración de Cotización crea el esquema `cotizacion` y su tabla de versiones vive dentro de él. En SQLite (desarrollo y pruebas unitarias) las tablas se crean directamente, sin esquema.
6. **Perfil detrás de un puerto.** El motor depende de `PerfilUnificado`. Un adaptador lo arma desde el perfil crudo de HU09; cuando HU10 entregue el perfil canónico solo se reemplaza el adaptador. El mapeo de nombres actual es provisional.
7. **Telemetría que no tumba la cotización.** `latencia_interna_ms` cubre perfil/caché, rating y persistencia de la cotización. El registro de latencia se escribe en una transacción aparte, después; si falla se registra el error y la cotización se entrega igual.

## Consecuencias

- **Supuesto de prototipo:** ninguna cifra de `v1.yaml` proviene de un estudio actuarial. Debe reemplazarla el área de negocio antes de cualquier uso real.
- **Contrato primero:** `docs/api/cotizacion.openapi.yaml` es la fuente de verdad para los canales; una prueba de contrato verifica que la implementación lo cumple.
- **Campos del producto en el contrato:** `datos` es un objeto genérico validado contra las reglas, no un `oneOf` por producto. Así agregar un ramo no cambia la API, a costa de que el contrato no enumere los campos de cada producto (están en los ejemplos).
- **Pendiente (HU14):** el desglose del precio no se persiste; `GET /v1/cotizaciones/{id}` devuelve `componentes: null`.
- **Pendiente (HU10):** mapeo definitivo de las señales de Open Finance al perfil unificado (nombres de la documentación vs. los de los simuladores).
