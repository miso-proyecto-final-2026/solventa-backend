# ADR-0005 — Esquema canónico del perfil unificado y normalización de proveedores

- **Estado:** Propuesto (prioridad de fuentes y rangos pendientes de confirmar con el equipo y negocio)
- **Fecha:** 2026-10-17
- **Historia:** HU10 (E01 · F01 Integración con sistemas de datos externos, Sprint 1)
- **Depende de:** [ADR-0003](0003-motor-de-rating-reglas-versionadas-y-migraciones-alembic.md) (el motor consume `PerfilUnificado`) y [ADR-0004](0004-patron-adaptadores-cambio-de-proveedor.md) (el contrato de cada proveedor vive en su adaptador).

## Contexto

HU09 entrega el perfil crudo de cada fuente (Open Finance, Open Data) con los nombres del contrato de cada proveedor, más marcas de calidad (`degradado`, `por_defecto`, `hit_cache`). El motor de rating (HU12) necesita una vista única y estable del cliente que no dependa de qué proveedor respondió.

## Decisión

1. **Un solo módulo conoce los nombres del proveedor:** `app/cotizacion/normalizacion.py`. Su tabla `NORMALIZADORES` mapea campo crudo → campo canónico; fuera de él solo existen nombres canónicos. Integrar o cambiar un proveedor es cambiar su entrada.
2. **Esquema canónico (`CAMPOS`)**, con unidad y rango válido por campo: `ingresos_mensuales_cop`, `ratio_endeudamiento`, `antiguedad_cuentas_anios`, `estrato`, `reportes_negativos`. Son los que consume hoy el motor; no se normalizan campos que nadie usa. Una prueba de contrato verifica que toda señal de `reglas/*.yaml` exista en el esquema.
3. **Cada campo trae su procedencia** (`fuente`, `obtenido_en` en ISO 8601 al serializar). La fecha la fija `ServicioPerfil` al consultar al proveedor y se guarda en caché junto al payload bajo una clave reservada (`__obtenido_en`) que no llega a los normalizadores; así sobrevive a la caché y al respaldo.
4. **Nunca se inventa un valor.** Un campo no disponible queda en `ausentes` con su motivo (`no_reportado`, `sin_datos`, `fuera_de_rango`, `invalido`, `degradado`, `sin_consentimiento`) y la `completitud` es la fracción de campos presentes. Los valores descartados se registran por nombre de campo y motivo, nunca por valor (dato financiero).
5. **Conflictos:** si dos fuentes aportan el mismo campo con valores distintos gana la de mayor prioridad (`PRIORIDAD`: Open Finance sobre Open Data) y se registra el `Conflicto`. Un valor inválido de la fuente prioritaria no gana: se usa el válido de la otra. Hoy los campos de ambas fuentes no se solapan; la regla se ejerce en pruebas con un normalizador alterno.
6. **Degradado:** el perfil conserva y propaga `degradado`. Los campos que solo vienen del respaldo (dato viejo) no se entregan: quedan ausentes con motivo `degradado`, igual que el comportamiento previo del motor, que aplica la regla conservadora.
7. **Pureza:** `normalizar()` no usa reloj, red ni estado; la misma entrada da el mismo perfil.
8. **Compatibilidad:** `PerfilUnificado` (contrato del motor) se deriva del perfil canónico y suma `completitud`. `senales` conserva su forma.

## Consecuencias

- **Supuestos de prototipo:** los rangos de `CAMPOS` y la prioridad de fuentes no vienen de negocio; se verifican con el equipo.
- **No se persiste ni se cachea el perfil canónico:** hoy se normaliza en cada solicitud a partir de las cachés por fuente (HU09). `PerfilCanonico.a_dict()` deja la forma serializable lista si HA01 decide cachear el perfil ya unificado.
- **Pendiente (HU11):** la trazabilidad de datos consultados puede reutilizar `fuente` y `obtenido_en`.
