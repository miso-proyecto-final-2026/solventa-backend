# ADR-0022 — MS Suscripción: revisión asistida

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-11-10
- **Historia:** HU16 (Sprint 3, F08/E04)
- **Depende de:** [ADR-0012](0012-ms-suscripcion-saga-decision-y-outbox.md) (saga, decisión y outbox), [ADR-0020 de venta asistida](0020-ms-suscripcion-venta-asistida.md) (JWT del asesor, auditoría) y [ADR-0008](0008-autenticacion-provisional-del-asesor-y-cotizacion-sin-cuenta.md) (el perfil solo se muestra por niveles)

## Contexto

HU15 deriva a una persona los casos que no cumplen las reglas automáticas: la saga queda en `EN_PROGRESO / DECISION`, se guarda una fila en `revisiones_asistidas` y se publica `revision_asistida_solicitada`. Nada consumía ese evento ni había rutas para ver o decidir esos casos. HU16 cierra el ciclo con la bandeja del asesor, el detalle, la decisión y la reanudación de la saga.

## Decisión

1. **Bandeja compartida.** Todo usuario con rol `ASESOR` ve todos los casos derivados; el filtro «Mis cotizaciones» limita a `suscripciones.asesor_id = yo`. Las rutas cuelgan de `/v1/suscripciones/revisiones` y usan el JWT del asesor; el token del cliente no sirve (401) y un `OPERADOR` recibe 403.
2. **Prioridad por antigüedad en la bandeja,** con umbrales configurables (`REVISION_PRIORIDAD_ALTA_HORAS=24`, `REVISION_PRIORIDAD_MEDIA_HORAS=4`): ALTA con 24 h o más, MEDIA con 4 h o más, BAJA en otro caso. Los más antiguos van primero. **Los umbrales son provisionales** y deben validarse con el negocio.
3. **Motivos en tres categorías,** sin crear reglas nuevas: `PERFIL_FUERA_DE_RANGO` (reportes negativos), `MONTO_SOBRE_LIMITE` y `DATOS_INCOMPLETOS_O_INCONSISTENTES`. Esta última agrupa perfil no disponible, sin consentimiento, incompleto o con un dato faltante, porque HU15 no tiene ninguna regla de inconsistencia real (no usa los `conflictos` de Perfilamiento); una regla así cambiaría las reglas de HU15, fuera de alcance. Una prueba pasa los mensajes reales de `decision.py` por `categorizar` en modo estricto, de modo que un motivo nuevo sin categoría hace fallar el CI.
4. **Tres decisiones, con justificación obligatoria** (20 a 2000 caracteres, interna: el cliente no la ve y el evento publicado no la incluye).
5. **Ajustar baja el monto asegurado y la prima se escala en proporción** (`factor = nuevo / actual`; coberturas, `prima_anual` y `prima_mensual` en pesos enteros; el deducible no cambia). La saga va directo al cobro de la prima ajustada, sin pedir otra aceptación al cliente. **Es una decisión provisional con dos riesgos aceptados:**
   - el cliente autorizó un monto mayor, así que el cobro menor queda cubierto, pero no aceptó expresamente la cobertura reducida;
   - la prima proporcional es una aproximación: ignora la `prima_minima` y los factores del motor de Cotización.

   El `AJUSTADA` automático de HU15 sigue esperando la aceptación del cliente (HU17); este es otro camino.
6. **La decisión humana sobrescribe `suscripciones.decision`** (APROBADA, AJUSTADA o RECHAZADA) para que el `GET` del cliente y móvil la vean «como automática». El `DERIVADA` original queda en `revisiones_asistidas` y en el evento del outbox. `decidido_en` y `duracion_decision_ms` siguen siendo los de la decisión automática (métrica de latencia de HU15). Al rechazar, `motivos` suma la frase genérica «Rechazada tras revisión asistida.».
7. **Reanudación interna más un evento informativo.** HU18 solo consume `solventa.suscripcion.comandos`, así que aprobar o ajustar encola el mismo comando `cobro_prima_solicitado` de HU15 (se extrajo `encolar_cobro`, compartido) y deja la saga en `APROBADA / COBRO`. Rechazar deja `RECHAZADA / DECISION`, sin comando; `RECHAZADA` ya libera la cotización. En ambos casos se limpia el token de pago. Además se publica `revision_asistida_resuelta` en `solventa.suscripcion.revision`, como hecho de dominio, por trazabilidad. Nadie lo consume por ahora.
8. **Una sola decisión.** Decidir corre en una transacción que bloquea la saga (`FOR UPDATE`) y luego la revisión abierta. Un índice único parcial `uq_revision_abierta (suscripcion_id) WHERE decision IS NULL` garantiza un solo caso abierto por saga. La segunda decisión recibe 409 `REVISION_YA_DECIDIDA` con la decisión, el asesor y la fecha en `details`.
9. **Inmutabilidad.** Un trigger de PostgreSQL impide el UPDATE de una revisión con `decision` no nula y todo DELETE, y un listener del ORM hace lo mismo, como en `auditoria_asesor` (migración 0005). La migración `0007` es reversible.
10. **Cotización vencida: bloquea aprobar y ajustar, no rechazar.** El valor que se cobraría ya no es válido. HU15 solo valida la vigencia al crear la saga, así que HU16 la vuelve a validar al decidir (409 `COTIZACION_VENCIDA`, la saga no cambia y el intento queda auditado). **Rechazar sí se permite:** es la única salida de un caso vencido y libera la cotización para que el cliente vuelva a cotizar; si no, el caso quedaría abierto para siempre.
    - La vigencia se guarda en la saga (`suscripciones.cotizacion_vigente_hasta`, que `iniciar_como` llena desde ahora) para que la bandeja marque y filtre los vencidos en SQL, sin llamar a Cotización por cada fila.
    - En las revisiones abiertas anteriores a la migración, la columna se llena la primera vez que hace falta, consultando Cotización (como máximo 50 por listado). Si Cotización no responde, aprobar y ajustar fallan cerrados con 503 `COTIZACION_NO_DISPONIBLE`; rechazar sigue funcionando.
    - El backend calcula `puede_decidir`, `cotizacion_vencida` y `decisiones_permitidas`, para que la web no copie la regla.
11. **Auditoría.** Cada intento de decidir queda en `auditoria_asesor` con `accion = DECIDIR_REVISION` y `EXITO` o el código del error, también los rechazados. La revisión guarda además `asesor_id`, `asesor_nombre` (claim del JWT), fecha, resultado, justificación y ajuste. Los logs registran ids y decisión, nunca la justificación, el token ni datos personales.

## Consecuencias

- **Sin cambios de infra:** el borde ya publica `/v1/suscripciones` y Suscripción ya produce en `solventa.suscripcion.revision`. No hay consumidores nuevos.
- **Móvil no consulta la saga en «En revisión».** El cliente no ve la decisión hasta volver a abrir la app: si se aprueba, la póliza aparece en «Mis pólizas» y llega el correo de emisión. **Un rechazo no se le notifica.** Queda abierto (fuera de esta historia).
- Los `AJUSTADA` automáticos de HU15 siguen sin poder avanzar (no hay endpoint de aceptación) y no entran a la bandeja, porque no son `DERIVADA`.
- El filtro por categoría se hace en memoria, porque la categoría sale del texto de los motivos. Es aceptable con una bandeja pequeña; si crece, habría que persistir las categorías.
- Se actualiza la nota del ADR-0012 §3: `DERIVADA` ya no espera indefinidamente, la resuelve un asesor.
- Abrir el detalle en la web llama al perfil del asesor en Cotización, que escribe la caché de perfil compartida; el cliente ya cotizó, así que no empeora nada.
- Alternativas descartadas: bandeja solo para el asesor de la cotización (los casos sin asesor quedarían huérfanos); reabrir la cotización al vencerse (la vigencia la define Cotización); pedir otra aceptación al ajustar (alarga la saga y no tiene pantalla ni endpoint).
