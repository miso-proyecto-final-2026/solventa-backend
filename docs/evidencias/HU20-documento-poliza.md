# HU20 — Evidencia del documento de la póliza (MS Suscripción)

- **Historia:** HU20, documento de la póliza tras el pago confirmado, Sprint 2 (F10, RQ-L2/EC-L2.1, RQ-D2)
- **Fecha:** 2026-10-28
- **Alcance de la evidencia:** pruebas automatizadas en local (Windows, Python 3.11, con PostgreSQL 16 y Kafka reales en contenedores), la colección de
  Postman con Newman y una prueba de humo con los servicios en ejecución. **Todavía no hay evidencia en staging**: falta aplicar infra y desplegar (lo hace una persona).
  Este informe se actualiza cuando exista.
- **Decisión de diseño:** [ADR-0015](../adr/0015-ms-suscripcion-documento-de-poliza.md)

## Criterios de aceptación

| # | Criterio | Cómo se verifica | Resultado |
|---|---|---|---|
| 1 | Documento tras el pago, con todos los datos | `tests/test_polizas.py`: extrae el texto del PDF y comprueba número, vigencia, cliente (nombre, tipo y documento enmascarado), tipo de póliza, valor asegurado, naturaleza del riesgo, amparos, coberturas con monto y deducible («No aplica» si es 0), prima anual y mensual, periodicidad, referencia del cobro y condiciones. Un caso por producto | pasa |
| 2 | Número único, sin reutilizar ni con reintentos | 20 caracteres del alfabeto de Crockford; 500 números distintos; colisión simulada → otro número; tres colisiones → error sin asignar; el reintento del PDF conserva el número y la clave | pasa |
| 3 | Idempotencia | Reentregas del `cobro_prima_confirmado` (con otro `event_id`) → una póliza y el mismo número; `registrar_poliza` dos veces → la misma fila. Con PostgreSQL real y 8 réplicas (`tests/test_postgres.py`): reentregas concurrentes → una póliza; varias réplicas del generador → un solo documento | pasa |
| 4 | Almacenamiento cifrado y versionado, con `documento_s3_key` | `tests/test_polizas_s3.py` y `tests/test_formato_y_almacen.py` con Moto: objeto con `ServerSideEncryption=AES256`, `VersionId` y dos versiones tras un reintento; la póliza guarda la clave | pasa (Moto). Bucket real: pendiente de staging |
| 5 | Consulta | `tests/test_api_polizas.py`: `GET /v1/suscripciones/{id}/poliza` (200, 404 sin póliza, 404 ajena igual que inexistente, 401) y `GET /v1/polizas?cliente_id=` (200, vacía, 403 de otro cliente, 422, 401). Sin URL de descarga | pasa |
| 6 | Evento `poliza_emitida` | `tests/test_contrato_poliza_emitida.py` y `tests/contratos/poliza_emitida.json` (generado con el código real): forma, clave `poliza_id`, `trace_id` y `correlation_id`, sin datos personales. `emitir_poliza` exige el documento generado y es idempotente. **Nada la dispara todavía** (HU21) | pasa |
| 7 | Presupuesto de decisión + emisión | La generación es asíncrona: el consumidor solo registra la póliza. Ver «Latencia» | documentado; *a confirmar* con negocio |
| 8 | Falla de generación | Almacén caído → reintentos con espera creciente → `FALLIDO` tras el máximo, con `documento_error` visible en el `GET` de la saga, un log de error sin datos personales y la saga en `COBRO_REALIZADO` / `FIRMA` con el cobro `APROBADO` | pasa |
| 9 | Contenido fiel | Se guarda una copia de las coberturas y de `version_reglas` al iniciar la saga; las primas salen de la saga, sin recalcular. Las sagas anteriores vuelven a leer la cotización (solo lectura) | pasa |

## Latencia de generación (criterio 7)

| Medida | Resultado |
|---|---|
| Renderizado del PDF (`renderizar_poliza`, 300 repeticiones, producto de dispositivos) | p50 5,3 ms · p95 7,4 ms · p99 9,1 ms · máx 13,3 ms |
| Prueba de humo (cobro confirmado → póliza `GENERADO`, SQLite y carpeta local) | 0,83 s en total, de los cuales ~0,7 s son la espera del proceso de fondo (sondea cada 1 s); generar y guardar tomó 15 ms |
| Efecto sobre la confirmación del pago | Ninguno: `registrar_poliza` solo inserta una fila dentro de la transacción del consumidor |

El renderizado cabe de sobra en el p95 ≤ 1,5 s / p99 ≤ 3 s del presupuesto, pero se mantiene asíncrono para que una falla de
S3 o de Identidad no toque el pago. En staging habrá que añadir la latencia de Identidad y de S3.

## Seguridad y datos personales

- El nombre y el documento del cliente no se guardan en la base de Suscripción: se piden a Identidad al generar y solo quedan
  dentro del PDF. El documento va enmascarado (solo los 3 últimos dígitos).
- La clave de S3 (`polizas/<suscripcion_id>/<numero_poliza>.pdf`) no lleva datos personales.
- Logs JSON con `trace_id` y `correlation_id`. `test_los_logs_de_la_generacion_no_traen_datos_personales` y `test_agotados_los_reintentos_queda_fallida…` comprueban que ni el
  nombre, ni el apellido ni el documento aparecen en los logs; un filtro enmascara además tokens `tok_…` y números largos.
  Prueba de humo: el log del proceso no contiene el nombre ni el apellido del cliente.
- Identidad: el endpoint interno exige la clave de servicio; un token de cliente no lo reemplaza (401); la respuesta no trae
  correo, celular ni el documento completo.

## Resultados de las pruebas

| Comando | Resultado |
|---|---|
| `pytest --cov=app` en `services/suscripcion` | **227 passed, 1 skipped** (con Docker: PostgreSQL 16 y Kafka reales; la omitida comprueba permisos POSIX `0600` y no aplica en Windows). Línea base antes de los cambios: 149 passed, 22 skipped sin Docker. Cobertura 97 % de `app` y de `app/suscripcion` (umbral del CI: 80 %) |
| `pytest` en `services/identidad` | 215 passed, **1 failed**: `test_kyc_almacen.py::test_el_almacen_local_guarda_el_contenido_con_permisos_restringidos`. Es de HU02 y comprueba permisos POSIX `0600`, que Windows no respeta; falla igual sin los cambios de HU20 |
| `ruff check .` (raíz del backend) | sin hallazgos |
| Prueba de humo (uvicorn: Identidad, Cotización, Perfilamiento y Suscripción) | cliente → cotización → suscripción → cobro confirmado inyectado → PDF de 2 páginas en la carpeta local; `GET …/poliza` 200, billetera 200, billetera ajena 403, resumen de Identidad 200 con documento enmascarado |

## Limitaciones y pendientes

- Con Docker se ejecutaron las pruebas de PostgreSQL real: carreras entre réplicas, migración reversible y sin deriva frente a los
  modelos, y el `CHECK` del formato del número.
- **Newman** con la colección completa y los seis servicios en local (Identidad, Cotización, Perfilamiento, Suscripción,
  Siniestros y Pagos, con bases SQLite limpias): 143 peticiones, 308 aserciones, 0 fallos, incluidas las carpetas nuevas de HU20.
  Una primera pasada tuvo 8 fallos en la carpeta de Perfilamiento (HU23) porque ese servicio se arrancó con `IDENTIDAD_URL`
  y exige consentimiento real; arrancado como indica el README pasó completa. El camino 200 de la póliza no está en Postman
  (necesita el cobro por Kafka) y se cubre con pytest y la prueba de humo.
- En la prueba de humo el perfil de desarrollo deriva la decisión a una persona, así que la saga se dejó a mano en
  `APROBADA` / `COBRO` antes de inyectar el cobro. El cobro confirmado en staging llega por Kafka desde MS Pagos.
- **Staging:** aplicar infra (`platform` → `baseline` → `cicd/deploy-staging`), recorrer el flujo por CloudFront, adjuntar un PDF de
  ejemplo (`aws s3 cp`) al issue y desmontar con `verificar-teardown.sh`. Requiere HU18 desplegado.
- A confirmar: datos del bien asegurado en el documento, vigencia de 12 meses, retención en producción, si la billetera muestra
  las pólizas `PENDIENTE_FIRMA`, reintento manual ante `FALLIDO` y URL de descarga (HU22).
