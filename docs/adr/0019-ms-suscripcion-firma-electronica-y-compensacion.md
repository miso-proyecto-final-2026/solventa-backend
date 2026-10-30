# ADR-0019 — MS Suscripción: firma electrónica de la póliza y compensación del cobro

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-30
- **Historia:** HU21 (Sprint 2, F10). Requisito RQ-I2. Alimenta a HU22 (entrega) y HU39 (firma asistida en web)
- **Depende de:** [ADR-0004](0004-proveedores-externos-detras-de-interfaces.md) (proveedores detrás de una interfaz), [ADR-0012](0012-ms-suscripcion-saga-decision-y-outbox.md) (saga y cola de salida), [ADR-0014](0014-ms-pagos-cobro-idempotente-y-conciliacion.md) (cobro idempotente) y [ADR-0017](0017-ms-suscripcion-documento-de-poliza.md) (documento de la póliza)

## Contexto

Tras HU20 la saga espera en `COBRO_REALIZADO` / `FIRMA` con el PDF generado. Falta que el cliente lo revise y lo firme con
evidencia de no repudio, que la saga avance a la emisión y, si la firma falla o vence, que **devuelva el cobro**. La
documentación no fija el mecanismo de autenticación reforzada ni el plazo para firmar, y MS Pagos todavía no sabe reembolsar.

## Decisión

1. **Mismo API para móvil y web.** `GET …/{id}/documento-firma` (hash, vencimiento, estado) y `…/documento-firma/pdf`;
   `POST …/{id}/firma/codigo` (envía el código); `POST …/{id}/firma`; `GET …/{id}/firma` (evidencia) y
   `GET …/{id}/firma/documento` (copia firmada). Una firma hecha en un canal se ve en el otro porque ambos leen la misma fila.
2. **Qué se firma: el hash del PDF.** HU20 guarda el SHA-256 del PDF al generarlo (`polizas.documento_sha256`; las pólizas
   anteriores lo calculan la primera vez). El cliente devuelve el hash que vio. Un hash distinto, o un PDF guardado cuyo hash ya
   no coincide, se rechaza con `DOCUMENTO_ALTERADO` y **no gasta un intento de código**.
3. **Autenticación reforzada: OTP simulado.** El puerto `ProveedorFirma` (ADR-0004) tiene un adaptador simulado que acepta un
   código fijo (`FIRMA_SIM_OTP`, `123456` por defecto). La biometría queda fuera. Tres códigos erróneos
   (`reglas/v1.yaml › firma.max_intentos_autenticacion`) dan la firma por fallida. El código nunca se escribe en un log.
4. **Vencimiento: 24 h desde el cobro** (`firma.vigencia_horas`, provisional como el resto de las reglas). Se calcula al
   confirmarse el cobro (`suscripciones.firma_vence_en`) y un barrido compensa las sagas vencidas aunque el cliente no vuelva.
5. **Evidencia inmutable.** Tabla `firmas` con `suscripcion_id` y `poliza_id` únicos: hash, fecha y hora, `cliente_id`
   (no su nombre), canal, IP (la primera de `X-Forwarded-For`), dispositivo, método, proveedor y referencia. Un disparador de
   PostgreSQL rechaza `UPDATE` y `DELETE`, y un oyente del ORM hace lo mismo en las pruebas con SQLite.
6. **Copia firmada.** El PDF original, página por página, más una página de constancia con la evidencia, en
   `firmadas/<suscripcion_id>/<numero>.pdf` del bucket de evidencias (versionado y cifrado). El hash del original sigue siendo
   verificable. La constancia dice que la firma es simulada y no tiene validez legal certificada.
7. **Tres pasos, como HU20.** (1) validar, autenticar y guardar la copia en el almacén, sin transacción abierta; (2) registrar la
   firma y pasar la saga a `FIRMADA` / `EMISION`; (3) emitir (`emitir_poliza`, de HU20). Si el proceso cae entre 2 y 3, el
   barrido emite las sagas `FIRMADA`. La emisión se **reclama con un `UPDATE … WHERE estado_saga = 'FIRMADA'`**, de modo que
   solo una réplica la logra aunque varias lo intenten a la vez.
8. **Idempotencia.** Firmar de nuevo devuelve la misma firma (cabecera `Idempotent-Replay`) sin pedir otro código ni emitir otra
   vez. Con un hash distinto responde `DOCUMENTO_ALTERADO`. La restricción única decide la carrera entre réplicas.
9. **Compensación por reembolso.** Si la firma vence o falla, la saga encola `reembolso_cobro_solicitado` (clave
   `reembolso_prima:<suscripcion_id>`) y marca `compensacion_estado = SOLICITADA`; **la saga sigue en `COBRO_REALIZADO`** hasta que MS
   Pagos confirme. MS Pagos registra una transacción `REEMBOLSO_PRIMA` sobre el cobro aprobado, la procesa con la misma
   maquinaria de reintentos y publica `cobro_prima_reembolsado`; entonces la saga queda `COMPENSADA`, la póliza `ANULADA` y la
   cotización se libera. Un comando sin cobro aprobado va a la cola de fallidos (`.dlq`) para revisión humana.
10. **La pasarela simulada reembolsa siempre** (salvo caída) y es idempotente por la clave del reembolso. Su libro distingue
    cobros de reembolsos (`simulador_cargos.tipo`), de modo que «cobros hechos» no cuenta las devoluciones.

## Consecuencias

- Los topics y permisos de Kafka no cambian: el reembolso viaja por `solventa.suscripcion.comandos` y su resultado por
  `solventa.pagos.resultados`.
- **Infra pendiente:** el rol del pod de Suscripción necesita leer (`s3:GetObject`) y escribir en `polizas/` y `firmadas/` del
  bucket de evidencias. El módulo `evidence-bucket` hoy es de solo escritura y la cuenta de servicio de Suscripción aún no
  figura en él.
- Un reembolso rechazado por la pasarela no se anuncia: queda `RECHAZADA` y visible en la base para el operador.
- Alternativas descartadas: biometría (sin proveedor ni requisito claro); firmar el PDF con un certificado real (fuera del
  alcance, EDT); compensar dentro del mismo consumidor del cobro (un reembolso no se deshace, ADR-0014).
