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
- **Despliegue:** `solventa-infra` registra el servicio como interno (sin `edge`) y el flujo `deploy-staging.yml` (comentario `cicd/deploy-staging` en un PR que modifique el servicio) lo publica en su propio repositorio ECR, corre la migración desde la plantilla `solventa-perfilamiento-migrate` y hace el rollout, con su propio rol OIDC (clave `solventa-perfilamiento` de la variable de repositorio `AWS_DEPLOY_ROLE_ARNS`). Solo corre si la variable existe. Hasta que staging esté encendido y el baseline aplicado, el job no tiene a qué desplegar.
- **Duplicación:** la copia del breaker, la caché y el cliente de consentimiento puede divergir de la de Cotización. Las pruebas de cada copia viajan con ella.
- **Retención:** no se define; las tres tablas crecen sin límite hasta acordar la política de datos.
- **Pendiente:** los simuladores devuelven valores fijos; la integración con proveedores reales (T4) y la oferta personalizada (HU24–HU26) quedan fuera de esta historia.

## Enmienda HU24 (2026-11-14): linaje completo, consultable por el asesor y auditado

HU24 (#79) pide que el linaje sea completo y reconstruible y que el asesor lo consulte en la consola.

1. **Más campos, sin migrar los existentes.** Cada línea del linaje agrega `tipo_dato` (`FINANCIERO` para Open Finance, `SOCIOECONOMICO` para Open Data: es lo que decide el TTL) y `estado_consentimiento`. Este último es siempre `OTORGADO`, porque sin ese estado no se enriquece (punto 5 de la Decisión), así que se deriva en lugar de guardarse. La respuesta agrega `ausentes` (campo → motivo) y `conflictos` (campo, fuente ganadora, descartadas) del perfil guardado: con ellos y las líneas por fuente se explica cada valor del perfil sin mostrarlo.
2. **Un perfil concreto.** `GET …/linaje?perfil_id=` consulta un perfil anterior del mismo cliente; sin el parámetro sigue siendo el vigente. Un `perfil_id` de otro cliente responde 404.
3. **Quién consultó.** Nueva tabla `consultas_linaje` (revisión `0002`, solo inserción con el mismo trigger): cliente, perfil, origen (nombre de la clave de servicio), `consultado_por` (cabecera opcional `X-Consultado-Por`, la persona a cuyo nombre consulta el servicio) y resultado (`ENTREGADO` o `SIN_PERFIL`). Se registra también la consulta sin perfil. Si el registro falla, la consulta falla.
4. **El asesor entra por Cotización.** `GET /v1/asesor/clientes/{cliente_id}/linaje` en MS Cotización valida el JWT del asesor (ADR-0008) y consulta a Perfilamiento con una clave de servicio **propia**, enviando el id del asesor en `X-Consultado-Por`. Así la auditoría distingue a Cotización de Suscripción y sabe qué asesor miró. Se descartó validar el JWT en Perfilamiento: duplicaba el acceso del asesor y obligaba a publicar otra ruta en el borde.
5. **El linaje no puede tumbar la cotización.** Cotización es crítica (HA01) y el linaje es secundario, así que, a diferencia de sus otras dependencias (que fallan cerrado), si falta `PERFILAMIENTO_URL` o `PERFILAMIENTO_SERVICE_KEY` Cotización arranca normal, deja la advertencia `linaje_sin_configurar` en el log al iniciar y la ruta responde 503 `LINAJE_NO_DISPONIBLE`. Lo mismo si Perfilamiento está caído o rechaza la clave. Así el orden de despliegue entre backend e infra no rompe staging.
6. **No exige consentimiento** (se mantiene el punto 8 de la Decisión). Es provisional: V-12 (#106) decide el comportamiento tras revocar.
7. **Una clave por llamador.** En staging cada secreto es una sola cadena, así que `PERFILAMIENTO_API_KEYS` no puede guardar dos claves con nombre sin cambiar el secreto que ya usa Suscripción. Perfilamiento acepta además `PERFILAMIENTO_API_KEY_<NOMBRE>` (una variable por llamador, cada una de su propio secreto); el nombre, en minúsculas, es el origen. Cotización usa `PERFILAMIENTO_API_KEY_COTIZACION` y Suscripción sigue igual.
8. **Alcance del linaje.** Es el del último perfil enriquecido en Perfilamiento, que no siempre es el perfil con que Cotización calculó un precio: Cotización tiene su propia réplica de proveedores (HU09).
