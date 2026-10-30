# HU39 — Evidencia de la venta asistida (MS Suscripción)

- **Historia:** HU39, firma y emisión asistida en web, Sprint 2 (F10, RQ-I2)
- **Fecha:** 2026-10-30
- **Alcance de la evidencia:** pruebas automatizadas y recorridos en local (Windows, Python 3.11, con PostgreSQL 16 y Kafka reales en contenedores para las pruebas, y Kafka real con SQLite para los recorridos). **Todavía no hay evidencia en staging.** Este informe se actualiza cuando exista.
- **Decisión de diseño:** [ADR-0020](../adr/0020-ms-suscripcion-venta-asistida.md)

## Criterios de aceptación

| # | Criterio | Cómo se verifica | Resultado |
|---|---|---|---|
| 1 | Lista con saga, progreso y compensaciones | `tests/test_api_asistida.py`: lista por asesor, filtro, pasos, compensación | pasa |
| 2 | Gestionar firma | Firma en pantalla: pruebas y recorrido real | **parcial:** el correo espera a HU22 |
| 3 | Misma evidencia más el asesor | `firmas` con firmante, `asesor_id` y `modalidad`; la constancia del PDF nombra al asesor | pasa |
| 4 | Emisión, descarga y reenvío | Póliza `VIGENTE` tras firmar y descarga del PDF | **parcial:** el reenvío espera a HU22 |
| 5 | Solo en `COBRO_REALIZADO` | Fuera de ese estado, con compensación en curso o vencida: no gestionable y 409 al ejecutar | pasa |
| 6 | Rol, relación y auditoría | 401, 403 sin rol, 403 sin relación; auditoría de éxito y rechazo, inmutable también en PostgreSQL | pasa |
| 7 | Enlace seguro | No implementado | **pendiente con HU22** |
| 8 | Errores explicados (backend) | Códigos `OTP_INVALIDO`, `FIRMA_VENCIDA`, `FIRMA_FALLIDA`, `DOCUMENTO_ALTERADO` | **parcial:** falta enlace vencido |

## Pruebas

- Suscripción: `pytest --cov=app` → 308 passed, 1 skipped (permisos POSIX), cobertura 97 %, con PostgreSQL y Kafka reales. Incluye migración `0005` sin deriva y reversible.
- Cotización: `pytest` → 370 passed.
- `ruff check .` sin hallazgos.
- Newman, carpeta de HU39 con los servicios en ejecución: 6 peticiones, 8 aserciones, 0 fallos. No se corrió la colección completa.

## Recorrido real por API (local)

Identidad, Cotización, Perfilamiento, Pagos y Suscripción en ejecución, con Kafka real. El perfil del cliente se enriqueció a mano (ver «Limitaciones»).

Login del asesor → cotización → suscripción → `COBRO_REALIZADO` por Kafka → PDF → código erróneo (403, «intentos restantes: 2») → firma → `EMITIDA`, póliza `VIGENTE` → descarga.

- `firmas`: firmante = cliente, `ASISTIDA_PANTALLA`, `asesor_id` del asesor, canal `WEB`.
- `auditoria_asesor`: `INICIAR_SUSCRIPCION`, `DESCARGAR_DOCUMENTO`, `SOLICITAR_CODIGO`, `FIRMAR` (`OTP_INVALIDO`), `FIRMAR` (`EXITO`), `DESCARGAR_FIRMADA`.
- El token de pago no aparece en la auditoría, en la cola de salida publicada ni en los logs de Suscripción, Pagos, Identidad y Cotización.

## Limitaciones

- **Ningún servicio ni canal llama a `POST /v1/perfilamiento/{id}/enriquecer`.** Sin perfil enriquecido, la decisión deriva el caso: sin enriquecer a mano, el recorrido se detuvo en `DERIVADA`. Es previo a HU39.
- El OTP es simulado (ADR-0019) y `acepta_terminos` es una declaración del asesor (ADR-0020).
- Los recursos de AWS no se han aplicado ni probado: no hay evidencia de E2E-08 en staging.
