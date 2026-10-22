# ADR-0009 — MS Perfilamiento: enriquecimiento de perfil y réplica del código de proveedores

- **Estado:** Propuesto (TTL, esquema ampliado y política de réplica pendientes de confirmar con el equipo)
- **Fecha:** 2026-10-21
- **Historia:** HU23 (E06 · F14 Enriquecimiento de perfil de riesgo, Sprint 2)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (un servicio no importa código de otro), [ADR-0004](0004-patron-adaptadores-cambio-de-proveedor.md), [ADR-0005](0005-esquema-canonico-perfil-unificado.md) y [ADR-0007](0007-trazabilidad-de-consultas-a-proveedores-externos.md).

## Contexto

HU23 crea MS Perfilamiento, que combina señales de Open Finance y Open Data en un perfil de riesgo del cliente, con consentimiento, linaje auditable y las mismas protecciones validadas en HA01 (timeout duro, circuit breaker, caché con dispersión de TTL). MS Cotización ya tiene ese código en `app/proveedores/` (HU09) y la normalización (HU10). El issue deja abierto si se extrae a una librería compartida o se replica «sin acoplar despliegues».

## Decisión

1. **Se replica, no se comparte.** Perfilamiento tiene su propia copia de `circuit_breaker`, `cache`, `ttl`, `consentimiento`, `simuladores` y `normalizacion`, y una versión adaptada de `ServicioPerfil` (`ServicioFuentes`). Una librería común obligaría a versionar y desplegar ambos servicios juntos, contra el ADR-0001, para ~600 líneas que cambian poco. El costo es que una corrección en un servicio hay que trasladarla a mano; queda anotado en el encabezado de cada módulo replicado. Si el código diverge poco y aparece un tercer consumidor, se reevalúa extraerlo a un paquete versionado.
2. **Qué cambia respecto a la copia:**
   - `ServicioFuentes.consultar` no verifica el consentimiento: lo hace `Enriquecedor` antes, porque un intento sin consentimiento debe quedar registrado con su origen.
   - TTL por tipo de dato (`PROVEEDORES_TTL_OPEN_FINANCE_S`, 3.600 s; `PROVEEDORES_TTL_OPEN_DATA_S`, 86.400 s), con la misma dispersión de ±10–15 %. Son los valores del ejemplo de linaje del issue (supuesto, a confirmar).
   - Sin trazabilidad de consultas por proveedor (ADR-0007 es de Cotización) ni perfiles demo.
3. **Esquema canónico propio.** Se amplía el de ADR-0005 con `obligaciones_mensuales_cop`, `score_crediticio`, `productos_financieros` y `zona_riesgo`, que son las señales que nombra el issue. Los rangos son supuestos de prototipo. Cotización no cambia: su motor solo consume los cinco campos originales.
4. **Persistencia (`perfilamiento`), solo inserción:** `perfiles_enriquecidos` (JSON canónico, completitud, degradado, `consentimiento_id`, correlación), `linaje_fuentes` (por fuente: campos que aportó, `obtenido_en`, TTL, `hit_cache`, `degradado`, `por_defecto`) e `intentos_bloqueados`. Un nuevo enriquecimiento crea otro perfil; el vigente es el último. Perfil y linaje se escriben en una sola transacción. Triggers de PostgreSQL rechazan UPDATE y DELETE. Sin llaves foráneas hacia otros servicios.
5. **Sin consentimiento, sin consulta y con rastro (EC-S1.2).** Si el consentimiento no está `OTORGADO`, no se llama a ningún proveedor, se responde 403 `CONSENTIMIENTO_NO_VIGENTE` y se escribe el intento (cliente, operación, origen, estado, correlación). El registro es síncrono y, si falla, el fallo se propaga: no se responde «bloqueado» sin evidencia. «Revocado» e «inexistente» dan la misma respuesta.
6. **Revocación (criterio 9).** El consentimiento se verifica en **cada** operación, incluida la lectura del perfil, así que una revocación deja de entregar el perfil de inmediato; el límite de 5 minutos queda holgado. El perfil guardado no se borra (es evidencia de auditoría), solo deja de servirse.
7. **Origen del intento.** El acceso es por clave de servicio en `X-Service-Key`. `PERFILAMIENTO_API_KEYS` acepta pares `nombre=clave`; el nombre es el «origen» que queda en el intento bloqueado. Vacía = nadie accede. Provisional hasta los roles internos (F26), como ADR-0006.
8. **Linaje sin valores.** `GET …/linaje` devuelve fuentes, campos (solo nombres), fechas y TTL, nunca los valores, y no exige consentimiento: sirve para auditar también a un cliente que luego revocó.

## Consecuencias

- **Rendimiento (criterio 8):** no está medido. Hay un humo k6 (`tests/rendimiento/humo-enriquecimiento.js`) pero la corrida de 20.000 perfilamientos/hora, y confirmar que la cotización en línea conserve su p95, requiere el servicio desplegado en staging. Cada enriquecimiento escribe 1 perfil + 2 filas de linaje.
- **Despliegue:** `solventa-infra` registra el servicio como interno (sin `edge`) y el job `deploy-staging-perfilamiento` del CI lo publica en su propio repositorio ECR, corre la migración desde la plantilla `solventa-perfilamiento-migrate` y hace el rollout, con su propio rol OIDC (variable de repositorio `AWS_ROLE_ARN_PERFILAMIENTO`). Solo corre en `main` y si la variable existe. Hasta que staging esté encendido y el baseline aplicado, el job no tiene a qué desplegar.
- **Duplicación:** la copia del breaker, la caché y el cliente de consentimiento puede divergir de la de Cotización. Las pruebas de cada copia viajan con ella.
- **Retención:** no se define; las tres tablas crecen sin límite hasta acordar la política de datos.
- **Pendiente:** los simuladores devuelven valores fijos; la integración con proveedores reales (T4) y la oferta personalizada (HU24–HU26) quedan fuera de esta historia.
