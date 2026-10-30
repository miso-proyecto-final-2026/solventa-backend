"""Paso COBRO de la saga (HU18): aplica el resultado que publica MS Pagos en `solventa.pagos.resultados`.

- `cobro_prima_confirmado`: la saga pasa a `COBRO_REALIZADO` y su siguiente paso es la firma (HU21). En la misma
  transacción se registra la póliza (HU20); el documento lo genera un proceso aparte, así el pago nunca espera al PDF.
- `cobro_prima_reembolsado` (HU21): MS Pagos devolvió el cobro de una firma fallida o vencida; la saga queda `COMPENSADA`.
- `cobro_prima_rechazado`: la saga queda en `APROBADA` / `COBRO` con el motivo y la fecha límite del período de gracia;
  qué hacer después (reintentar o compensar) es de HU19.

El consumidor de la plantilla ya descarta las reentregas por `event_id`; además, aplicar un resultado a una saga que
ya salió del paso COBRO no hace nada, así que una repetición de MS Pagos (otro `event_id`) también es inofensiva.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.eventos import Evento
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.dominio import EstadoSaga, PasoSaga
from app.suscripcion.firma import COMPENSACION_SOLICITADA, completar_compensacion
from app.suscripcion.modelos_db import SuscripcionDB
from app.suscripcion.polizas import registrar_poliza
from app.suscripcion.reglas import cargar_reglas

log = logging.getLogger("suscripcion.cobros")

CONFIRMADO = "cobro_prima_confirmado"
RECHAZADO = "cobro_prima_rechazado"
REEMBOLSADO = "cobro_prima_reembolsado"


def _fecha(valor: Any) -> datetime | None:
    if not valor:
        return None
    fecha = datetime.fromisoformat(str(valor))
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)


async def manejar_resultado_cobro(evento: Evento, sesion: Session) -> None:
    """Manejador del consumidor `solventa-suscripcion.pagos` (corre en la transacción de la plantilla)."""
    if evento.event_type not in (CONFIRMADO, RECHAZADO, REEMBOLSADO):
        log.info("evento ignorado event_type=%s event_id=%s", evento.event_type, evento.event_id)
        return
    datos = evento.payload
    susc = sesion.get(SuscripcionDB, datos.get("suscripcion_id"), with_for_update=True)
    if susc is None:
        log.warning("resultado de cobro para una suscripción inexistente event_id=%s", evento.event_id)
        return
    if evento.event_type == REEMBOLSADO:
        if susc.compensacion_estado == COMPENSACION_SOLICITADA:
            completar_compensacion(sesion, susc, datetime.now(UTC))
            log.info("cobro reembolsado suscripcion_id=%s estado_saga=%s", susc.id, susc.estado_saga)
        else:
            log.info("reembolso ignorado suscripcion_id=%s compensacion=%s", susc.id, susc.compensacion_estado)
        return
    if (susc.estado_saga, susc.paso_actual) != (EstadoSaga.APROBADA.value, PasoSaga.COBRO.value):
        log.info("resultado de cobro ignorado suscripcion_id=%s estado=%s paso=%s", susc.id, susc.estado_saga, susc.paso_actual)
        return

    ahora = datetime.now(UTC)
    susc.cobro_transaccion_id = datos.get("transaccion_id")
    susc.cobro_referencia = datos.get("referencia_pasarela")
    susc.cobro_resuelto_en = _fecha(datos.get("resuelta_en")) or ahora
    susc.updated_at = ahora
    if evento.event_type == CONFIRMADO:
        susc.cobro_estado = "APROBADO"
        susc.cobro_codigo = susc.cobro_motivo = susc.cobro_gracia_hasta = None
        susc.estado_saga = EstadoSaga.COBRO_REALIZADO.value
        susc.paso_actual = PasoSaga.FIRMA.value
        susc.firma_vence_en = susc.cobro_resuelto_en + timedelta(hours=cargar_reglas().firma_vigencia_horas)
        registrar_poliza(sesion, susc, ahora, vigencia_meses=ConfigSuscripcion.desde_entorno().polizas_vigencia_meses)
    else:
        susc.cobro_estado = "RECHAZADO"
        susc.cobro_codigo = (datos.get("codigo") or "RECHAZADO")[:50]
        susc.cobro_motivo = (datos.get("motivo") or "")[:200] or None
        susc.cobro_gracia_hasta = _fecha(datos.get("gracia_hasta"))
    log.info("cobro aplicado suscripcion_id=%s cobro=%s estado_saga=%s", susc.id, susc.cobro_estado, susc.estado_saga)
