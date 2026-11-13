# HU16 — Evidencia de la revisión asistida (MS Suscripción y consola web)

- **Historia:** HU16, derivación a revisión asistida, Sprint 3 (F08/E04)
- **Fecha:** 2026-11-10
- **Alcance de la evidencia:** pruebas automatizadas en local (Windows, SQLite en memoria para backend). Las pruebas con PostgreSQL y Kafka reales (contenedores) sí se ejecutaron. **Todavía no hay evidencia en staging.** Este informe se actualiza cuando exista.
- **Decisión de diseño:** [ADR-0022](../adr/0022-ms-suscripcion-revision-asistida.md)

## Criterios de aceptación

| # | Criterio | Cómo se verifica | Resultado |
|---|---|---|---|
| 1 | Bandeja con motivo, fecha y prioridad | `test_revision_asistida.py` (orden, prioridad, filtros), `test_revision.py` (límites); web: specs y Playwright | pasa |
| 2 | Detalle: cliente, cotización, perfilamiento, motivo | `test_la_bandeja_y_el_detalle_por_http`; web: `review-detail-page.spec.ts` (el perfil solo por niveles) | pasa |
| 3 | Aprobar, rechazar o ajustar con justificación | Servicio y HTTP por cada decisión; validación 422; Playwright | pasa |
| 4 | La saga continúa o se cierra sin cobro | `APROBADA/COBRO` con comando de cobro (monto original o ajustado); `RECHAZADA/DECISION` sin comando y cotización liberada | pasa |
| 5 | Trazabilidad inmutable | Listener del ORM probado; trigger de PostgreSQL en `test_postgres.py` | pasa |
| 6 | Una sola decisión | Segunda decisión → 409 con quién y cuándo; carrera de 8 réplicas en `test_postgres.py` | pasa |
| 7 | Motivos de la guía | `categorizar` con los mensajes reales de `decision.py` en modo estricto | pasa (inconsistencias agrupa datos faltantes) |
| 8 | Solo asesores | 401 sin token o con el token del cliente, 403 con OPERADOR; web: `roleGuard` y Playwright | pasa |
| 9 | es-CO, en-US y accesibilidad | Build `--localize` sin faltantes; Playwright en ambos idiomas; axe sin fallos graves | pasa |

## Pruebas

- Suscripción: `ruff check .` sin hallazgos; `pytest --cov=app` → 482 passed, 1 skipped, cobertura 97 %, con PostgreSQL 16 y Kafka reales en contenedores. `test_postgres.py`: 24 passed (migración `0007` sin deriva y reversible a `0006`, trigger, índice parcial, carrera de 8 réplicas con una sola ganadora). HU15, HU18, HU21 y HU39 sin cambios.
- Web: `ng lint` limpio; `npm run test:ci` → 409 passed; `ng build --localize` sin advertencias; Playwright → 75 passed.

- Newman (Identidad, Cotización y carpeta HU16, con Identidad, Cotización, Perfilamiento y Suscripción en local sobre SQLite y sin Kafka): 113 peticiones, 241 aserciones, 0 fallos. La carpeta HU16 cubre bandeja, detalle, aprobar, segunda decisión (409), justificación corta (422), caso inexistente (404), token del cliente (401), sin sesión (401) y OPERADOR (403). No se corrió la colección completa.

## Recorrido de punta a punta (local)

Identidad, Cotización y Perfilamiento (SQLite), Pagos y Suscripción sobre **PostgreSQL 16 y Kafka reales** (contenedores), con la migración `0007` aplicada por Alembic. Dos clientes nuevos, sin perfil enriquecido, cotizan (microseguro de vida, 50 000 000 COP, prima anual 175 000) y suscriben: ambos quedan `DERIVADA` por «perfil no disponible».

1. **Bandeja y detalle** (asesor demo): los dos casos aparecen `PENDIENTE`, prioridad `BAJA`, categoría `DATOS_INCOMPLETOS_O_INCONSISTENTES`, cliente con inicial del apellido y documento enmascarado; el detalle permite las tres decisiones y trae la vigencia de la cotización.
2. **Ajustar el caso 1** a 30 000 000 (factor 0,6): primas 105 000 anual y 8 750 mensual. La saga pasa a `APROBADA/COBRO`; el comando `cobro_prima_solicitado` (monto 105 000,00, token `[redactado]`) sale por Kafka, Pagos cobra, `cobro_prima_confirmado` vuelve y la saga queda `COBRO_REALIZADO/FIRMA` con la póliza `PENDIENTE_FIRMA` y coberturas escaladas (30 000 000 y 15 000 000).
3. **Segunda decisión** sobre el mismo caso: 409 `REVISION_YA_DECIDIDA`, con la decisión, el asesor y la fecha.
4. **Rechazar el caso 2**: saga `RECHAZADA/DECISION`, sin cobro (`pagos.transacciones` solo tiene el cobro de 105 000 del caso 1), token limpiado y cotización liberada.
5. **Registros:** `revisiones_asistidas` con decisión, asesor, justificación y ajuste; `auditoria_asesor`: `DECIDIR_REVISION` con `EXITO`, `REVISION_YA_DECIDIDA` y `EXITO`; `eventos_salida`: `revision_asistida_solicitada` x3, `cobro_prima_solicitado` y `revision_asistida_resuelta` x2, todos publicados. La justificación no aparece en ningún evento ni en los logs de los servicios; el token de pago tampoco.
6. **Inmutabilidad en vivo:** `UPDATE` y `DELETE` directos sobre una revisión decidida fallan con «Una revisión asistida decidida es inmutable» / «no se puede borrar».

No se recorrió la firma ni la emisión de la póliza (fuera del alcance de HU16). La cotización vencida no se probó en este recorrido (pytest y Playwright la cubren).

## Pendiente

- Staging: el mismo recorrido por CloudFront y el `SELECT` de `auditoria_asesor` (DoD adicional), previa confirmación.
