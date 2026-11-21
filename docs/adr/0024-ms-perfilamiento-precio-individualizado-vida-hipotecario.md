# ADR-0024: MS Perfilamiento — precio individualizado del seguro de vida hipotecario

- **Estado:** Propuesto (HU25, Sprint 3). Las cifras de las reglas son supuestos de prototipo, pendientes de revisión de producto y actuaría.
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin acoplar despliegues),
  [ADR-0003](0003-motor-de-rating-con-reglas-versionadas.md) (reglas versionadas) y [ADR-0009](0009-ms-perfilamiento-fuentes-externas.md) (el perfil)

## Contexto

HU25 pide que, dado un cliente con perfil enriquecido y consentimiento vigente, Perfilamiento devuelva la oferta de vida hipotecario con una prima
ajustada a su perfil y a su etapa de vida. Los documentos no definen la fórmula, las variables ni los rangos de precio, y el producto no está en el
catálogo de Cotización (S6).

## Decisión

1. **Cálculo propio en Perfilamiento, no el motor de Cotización.** Reutilizar el motor obligaría a que Perfilamiento llamara a Cotización
   (o a copiarlo con su catálogo) y acoplaría los despliegues (ADR-0001). Se copia el *estilo* del motor (función pura, `Decimal`, redondeo
   `ROUND_HALF_UP` COP en cada paso, reglas en YAML validadas al cargar) y no su código. Si producto decide que la oferta debe pasar por el catálogo,
   se mueve el cálculo detrás del mismo contrato.
2. **Endpoint** `GET /v1/perfilamiento/{cliente_id}/oferta?suma_asegurada=` con `X-Service-Key`. Sin `suma_asegurada` se asume el ingreso mensual por
   60 meses (dentro de 20 M–800 M COP) y, sin ingresos en el perfil, 100 M. Fuera de los límites: 422 con el campo.
3. **Reglas versionadas** en `app/perfilamiento/reglas/vida_hipotecario_v1.yaml` y la respuesta lleva `version_reglas`. El cargador valida que los
   rangos sean contiguos y que los factores respeten el `sentido` de cada señal: el precio no baja cuando el riesgo sube.
4. **Fórmula.** `base = max(tasa × suma, prima mínima)`; luego el factor de la **etapa de vida**; luego el factor de cada señal del perfil
   (endeudamiento, reportes negativos, ingresos, score), en orden; y un recargo si el perfil es degradado (datos de respaldo).
   Una señal ausente aplica 1,00 y queda anotada como «sin dato»: no se inventa un valor.
5. **Etapa de vida.** El perfil no trae la edad; se aproxima con `antiguedad_cuentas_anios` (inicial < 5, desarrollo < 15, consolidación < 30,
   madurez). Es la decisión más débil: la edad real vendría de Identidad *(a confirmar con producto)*.
6. **Consentimiento y perfil.** Cada consulta verifica el consentimiento (sin él, 403 y el intento queda registrado con operación `OFERTA`) y usa el
   perfil enriquecido más reciente. Sin perfil responde 404 `PERFIL_NO_ENCONTRADO`; no enriquece en línea *(a confirmar)*.
7. **Auditoría.** Cada oferta se guarda (tabla de solo inserción `ofertas_calculadas`, migración `0002`) con la versión de reglas, el desglose paso a
   paso y el `perfil_id`, del que cuelga el linaje de sus fuentes. La base más los ajustes suman exactamente la prima anual.

## Consecuencias

- El precio es reproducible: mismo perfil, misma versión de reglas, misma prima. Cambiar una tarifa es publicar otro archivo.
- Las cifras no tienen respaldo actuarial. Hasta que producto y actuaría las revisen, la oferta es de demostración (definición de terminado de HU25).
- HU26 (la pantalla del asesor) consume este contrato.
