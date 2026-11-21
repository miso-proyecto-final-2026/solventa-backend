# HU25 — Tres perfiles de ejemplo con su oferta

Salida real del cálculo (`calcular_oferta`, reglas `2026.11.1-prototipo`, suma asegurada 200 000 000 COP). Las cifras son supuestos de prototipo
(ADR-0024), no tarifas aprobadas.

| Perfil | Etapa | Endeud. | Reportes | Ingresos | Score | Prima anual | Prima mensual |
|---|---|---|---|---|---|---|---|
| **A** · joven, bajo riesgo | inicial (3 años) | 0,15 → ×0,95 | 0 → ×1,00 | 3,5 M → ×1,00 | 780 → ×0,95 | **292 410** | 24 368 |
| **B** · perfil medio | desarrollo (6 años) | 0,32 → ×1,00 | 0 → ×1,00 | 4,5 M → ×1,00 | 710 → ×1,00 | **360 000** | 30 000 |
| **C** · madurez, alto riesgo | madurez (32 años) | 0,58 → ×1,25 | 4 → ×1,35 | 1,8 M → ×1,10 | 480 → ×1,20 | **1 403 326** | 116 944 |

Base común: 0,0018 × 200 000 000 = 360 000. La etapa multiplica 0,90 / 1,00 / 1,75 y cada señal el factor de la tabla.

Cómo se reproduce: `pytest tests/perfilamiento/test_precio.py` (los límites de cada regla, el determinismo y la propiedad «el precio no baja cuando el
riesgo sube») y `tests/perfilamiento/test_oferta.py` (el endpoint, el consentimiento, el desglose guardado y la latencia).

**Pendiente:** revisión de producto o actuaría de las reglas (definición de terminado de HU25) y medir la latencia con carga real en staging (HA02).
