# ADR-0029: La oferta de vida hipotecario se registra como cotización para la venta asistida

- **Estado:** Propuesto (HU26, Sprint 3), **a confirmar con el equipo**. Resuelve de forma provisional el habilitador S6
  (el producto no está en el catálogo de Cotización).
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin acoplar despliegues),
  [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (REST síncrono entre servicios),
  [ADR-0006](0006-desglose-del-precio-persistido-y-acceso-de-auditoria.md) (el desglose no lleva el valor de las señales) y
  [ADR-0024](0024-ms-perfilamiento-precio-individualizado-vida-hipotecario.md) (el precio de vida hipotecario, HU25).

## Contexto

HU26 pide que el asesor genere la oferta de vida hipotecario dentro de la consola y, si el cliente acepta, inicie la
suscripción asistida con los datos de esa oferta. Hay tres piezas que no encajan solas:

- El precio lo calcula **MS Perfilamiento** (HU25, `GET /v1/perfilamiento/{cliente_id}/oferta`), que exige la clave de
  servicio: la consola, con el JWT del asesor, no puede llamarlo.
- **La suscripción asistida (HU39)** solo acepta un `cotizacion_id` y lee la cotización desde Cotización
  (`GET /v1/cotizaciones/{id}`): producto, primas, coberturas, vigencia y asesor.
- **El catálogo de Cotización** solo tiene `microseguro_vida` y `proteccion_dispositivos` (S6).

## Decisión

1. **Cotización hace de fachada** hacia Perfilamiento, como con el linaje (HU24):
   `POST /v1/asesor/clientes/{cliente_id}/ofertas-vida-hipotecario`, con el JWT del asesor. Usa la clave de servicio
   propia de Cotización (`PERFILAMIENTO_SERVICE_KEY`, origen `cotizacion`) y envía a nombre de qué asesor consulta
   (`X-Consultado-Por`).
2. **Perfilamiento automático (criterio 1).** Con `enriquecer` (por defecto), la fachada primero enriquece el perfil
   (`POST …/enriquecer`) y luego pide la oferta. Al solo cambiar la suma asegurada, la consola manda
   `enriquecer: false` para no crear un perfil nuevo en cada cambio.
3. **La oferta se registra como cotización** del producto `vida_hipotecario`, con la prima y las coberturas que calculó
   Perfilamiento, canal `WEB_ASESOR` y el asesor. La vigencia es la de las cotizaciones (30 días, reglas de Cotización).
   Suscripción la lee como cualquier otra cotización y **no cambia**, salvo sus plantillas de documento, que ahora
   nombran el producto y el amparo `incapacidad_total_permanente`.
4. **El precio nunca viaja desde el navegador.** Cotización lo pide a Perfilamiento en la misma solicitud y no lo
   recalcula. El deducible pasa de porcentaje (Perfilamiento) a monto (Cotización y Suscripción), con redondeo
   `ROUND_HALF_UP`.
5. **Sin desglose propio en Cotización.** El desglose de la oferta queda en Perfilamiento (`ofertas_calculadas`); la
   cotización guarda en `datos` el `oferta_id`, el `perfil_id`, la suma y la etapa de vida para llegar a él. Al asesor
   se le muestran los pasos del precio (regla, factor y ajuste) **sin** `valor_perfil`, que trae cifras del cliente
   (ADR-0006).
6. **Errores.** Sin consentimiento: 409 `CONSENTIMIENTO_REQUERIDO` y no se registra nada (criterio 6). Perfilamiento
   caído, lento o con la clave rechazada: 503 `OFERTA_NO_DISPONIBLE`, que se puede reintentar (criterio 7). Suma fuera
   de los límites de las reglas: 422 con el motivo que da Perfilamiento. Sin URL o sin clave de Perfilamiento,
   Cotización arranca igual y la oferta responde 503 (HA01), como el linaje.

### Alternativa descartada

**Agregar `vida_hipotecario` al catálogo de Cotización con reglas propias.** Habría dos precios distintos para el mismo
producto (el de Perfilamiento, individualizado con el perfil, y el de Cotización), o habría que copiar las reglas de
HU25 en Cotización y mantenerlas sincronizadas. Si el equipo decide que la oferta debe pasar por el catálogo, se mueve el
cálculo detrás del mismo contrato de la fachada y la consola no cambia.

## Consecuencias

- La venta asistida de vida hipotecario reutiliza la suscripción, la firma y la emisión de HU39 sin cambios de código.
- Cada oferta generada deja una cotización. Una consulta del asesor que cambia la suma varias veces deja varias
  cotizaciones vigentes del mismo cliente; solo la que se suscribe se usa.
- `GET /v1/cotizaciones/{id}/desglose` no tiene desglose para estas cotizaciones (como las anteriores a HU14): la
  auditoría del precio se hace en Perfilamiento con el `oferta_id`.
- **Las reglas de decisión automática de Suscripción (HU15) son genéricas:** derivan a revisión humana cualquier monto
  asegurado mayor de 80 M COP y aprueban «ajustado» a 50 M entre 50 M y 80 M. Las sumas de vida hipotecario van de 20 M
  a 800 M, así que la mayoría de estas suscripciones irán a revisión asistida (HU16). Definir umbrales por producto
  queda pendiente, con producto y actuaría.

## A confirmar con el equipo

- Que la oferta se registre como cotización (este ADR) o que el producto entre al catálogo de Cotización.
- Los textos de la póliza y del certificado para `vida_hipotecario` (propuestos en las plantillas de Suscripción).
- Los umbrales de decisión automática para montos de vida hipotecario.
