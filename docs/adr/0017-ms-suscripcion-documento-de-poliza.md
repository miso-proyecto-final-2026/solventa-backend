# ADR-0017 — MS Suscripción: documento de la póliza tras el cobro confirmado

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-28
- **Historia:** HU20 (Sprint 2, F10). Requisitos RQ-L2 (EC-L2.1), RQ-D2. Alimenta a HU21 (firma y emisión) y HU22 (entrega)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (sin código compartido entre servicios), [ADR-0010](0010-eventos-sobre-msk-plantilla-por-servicio.md) (plantilla de eventos), [ADR-0012](0012-ms-suscripcion-saga-decision-y-outbox.md) (saga y cola de salida) y [ADR-0014](0014-ms-pagos-cobro-idempotente-y-conciliacion.md) (registrar, ejecutar tras confirmar, reintentar)

## Contexto

HU18 deja la saga en `COBRO_REALIZADO` / `FIRMA` cuando llega `cobro_prima_confirmado`. Nadie genera el documento de la póliza,
no existe `suscripcion.polizas` y nadie publica en `solventa.polizas.ciclovida`. El documento debe salir con número único,
datos del cliente y del producto, coberturas con monto y deducible, prima, vigencia y condiciones, y debe coincidir con la
cotización aceptada. Generar un PDF y subirlo a S3 es un efecto externo que puede fallar o tardar, y **el pago ya está
confirmado**: no puede bloquearse ni deshacerse por culpa del documento.

## Decisión

1. **Tres pasos, como el cobro de Pagos (ADR-0014).**
   - *Registrar* (en la transacción del consumidor del cobro): solo se inserta la fila de `polizas` con el número y la
     vigencia. Es barata e idempotente: `suscripcion_id` es `UNIQUE` y la saga solo se aplica una vez.
   - *Generar* (proceso de fondo): toma las pólizas con `documento_estado = PENDIENTE` con `FOR UPDATE SKIP LOCKED`, anota
     el intento y **reserva** la fila hasta el siguiente intento (si el proceso cae, la reserva vence y se reintenta); luego
     arma los datos, renderiza el PDF y lo guarda. Un error se reintenta con espera creciente
     (`POLIZAS_REINTENTO_BASE_S` … `POLIZAS_REINTENTO_MAXIMO_S`); agotados `POLIZAS_MAX_INTENTOS` (5) queda `FALLIDO`
     y se registra un log de error sin datos personales.
   - *Emitir* (`emitir_poliza`, la dispara HU21 tras la firma): exige el documento `GENERADO`, pasa la póliza a `VIGENTE`, la
     saga a `EMITIDA` / `EMISION` y encola `poliza_emitida` en `solventa.polizas.ciclovida` por la cola de salida, con la
     clave `poliza_id` y propagando `trace_id` y `correlation_id`. Nada la llama todavía.
2. **Generación asíncrona (criterio 7).** El pago nunca espera al PDF. Se mide la latencia de generación y se documenta en
   `docs/evidencias/HU20-documento-poliza.md`. El criterio queda *a confirmar* con negocio.
3. **Número de póliza:** 20 caracteres del alfabeto Base32 de Crockford en mayúsculas (sin `I`, `L`, `O` ni `U`), generados con
   `secrets` (unos 100 bits de aleatoriedad, sin contador compartido entre réplicas). `UNIQUE` y un `CHECK` de formato en la
   base. Una colisión (`IntegrityError` en un `SAVEPOINT`) genera otro número, hasta 3 intentos. **Se asigna una sola vez**, al
   registrar: los reintentos del PDF y las reentregas del evento reutilizan la fila, y las pólizas no se borran, así que un
   número nunca se reutiliza.
4. **Estados.** `polizas.estado`: `PENDIENTE_FIRMA` → `VIGENTE`. El esquema documentado del hub dice `DEFAULT 'VIGENTE'`;
   se desvía a propósito porque la póliza solo es vigente tras la firma de HU21. `polizas.documento_estado`:
   `PENDIENTE` → `GENERADO` | `FALLIDO`. La saga sigue en `COBRO_REALIZADO` / `FIRMA` hasta emitir. El `GET` de la suscripción
   muestra el bloque `poliza` (estado del documento, intentos y nombre del último error). Hoy ese `GET` solo acepta el token
   del cliente; el acceso del operador con el JWT del rol `OPERADOR` (HU19, ADR-0008) queda pendiente.
5. **Contenido fiel (criterio 9).** Al iniciar la saga se guarda una copia de `coberturas[{tipo, monto, deducible}]` y de la
   versión de reglas de la cotización (antes solo se guardaba el monto máximo). Para sagas anteriores a HU20 el generador relee
   la cotización por REST, solo lectura y sin recalcular. Las primas salen de `suscripciones`, nunca se recalculan.
6. **Datos del cliente.** Un endpoint interno nuevo de Identidad (`GET /v1/resumenes-cliente/{id}`, clave de servicio, fuera del
   borde) devuelve nombre, apellido, tipo de documento y el documento **enmascarado**. No se reutiliza
   `/v1/clientes/busqueda`: busca por documento y enmascara el apellido por diseño (HU38). Suscripción no guarda esos datos: solo
   quedan dentro del PDF, que se cifra en S3.
7. **Plantilla propia y versionada** (`plantillas/poliza/v1.yaml`, es-CO): por producto, el nombre y la naturaleza del riesgo;
   nombres legibles de los amparos y condiciones generales. `plantilla_version` se guarda en la póliza. Un producto sin
   plantilla no hace fallar la generación: usa la clave y un texto genérico, y lo registra. No hay una plantilla legal externa,
   así que esta es la definitiva y no lleva marca «de prueba».
8. **PDF con `reportlab`** (BSD, ruedas sin dependencias del sistema) y formato es-CO propio (`$ 1.234.567`, COP sin decimales
   como en Cotización; «26 de octubre de 2026» en hora de Colombia).
9. **Almacén detrás de un puerto** (`AlmacenDocumentos`, `AlmacenLocal` y `AlmacenS3`), copiado del patrón de
   `services/identidad/app/kyc/almacen.py` (ADR-0001 y ADR-0009: sin código compartido). `POLIZAS_BUCKET` elige S3 (con
   `ServerSideEncryption="AES256"`; respeta `AWS_ENDPOINT_URL`, así que en local sirve Moto); si falta, en desarrollo se usa la
   carpeta `POLIZAS_DOCUMENTOS_DIR`. Clave: `polizas/<suscripcion_id>/<numero_poliza>.pdf`, sin datos personales; un
   reintento sobrescribe la misma clave y crea una versión nueva.
10. **Consulta sin descarga.** `GET /v1/suscripciones/{id}/poliza` y `GET /v1/polizas?cliente_id=` devuelven metadatos y
    `documento_s3_key`, sin URL de descarga (es de HU22). Así el permiso del pod es solo de escritura. La póliza de otro
    cliente responde 404, igual que una inexistente; pedir la billetera de otro `cliente_id` es 403.

## Consecuencias

- **A favor:** el pago nunca se bloquea ni se revierte por el documento; los reintentos y las reentregas no duplican ni
  cambian el número; el permiso IAM es mínimo (`s3:PutObject` sobre `polizas/*`); el contenido del documento se puede
  verificar con pruebas que extraen el texto del PDF.
- **En contra / límites:**
  - La póliza existe desde el cobro, antes de la firma, con el documento aún por generar: la billetera puede mostrar
    `PENDIENTE_FIRMA` (a confirmar con negocio si debe verse).
  - El documento no incluye los datos del bien asegurado (tipo y antigüedad del dispositivo): Cotización no expone `datos` en
    `CotizacionOut`. Queda como mejora.
  - La vigencia de 12 meses desde el cobro (`POLIZAS_VIGENCIA_MESES`) es provisional: ningún documento la define.
  - Una póliza `FALLIDO` no se reintenta sola; el reintento manual del operador queda fuera de alcance. HU19 no lo cubre
    (gestiona los cobros fallidos en Pagos), así que queda pendiente, sin historia asignada.
  - La retención de las pólizas en producción está por definir (en staging el bucket no expira objetos).
