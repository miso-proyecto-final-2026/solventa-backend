"""Recepción de eventos paramétricos (HU27): validar, registrar, publicar y acusar recibo.

El acuse sale en cuanto el evento está registrado y publicado en Kafka; el procesamiento
(HU28) ocurre después y no lo espera. La publicación usa el cliente de la plantilla HS2
(`app/eventos`, ADR-0010).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiokafka.errors import KafkaError
from sqlalchemy.orm import Session

from app.contexto import correlacion_id
from app.eventos import Evento, PublicadorEventos
from app.siniestros.dominio import validar
from app.siniestros.errores import BusNoDisponible, EventoInvalido
from app.siniestros.repositorio import EventoGuardado, RepositorioEventos

log = logging.getLogger(__name__)

TOPIC_EVENTOS = "solventa.siniestros.eventos"  # clave de partición: poliza_id (contrato de infra)
TIPO_EVENTO = "evento_parametrico_recibido"
FUENTE_SOBRE = "ms-siniestros"
# El grupo de consumo debe empezar por el nombre del servicio: es lo único que permite su política de IAM.
GRUPO_RECEPCION = "solventa-siniestros.recepcion"


@dataclass(frozen=True)
class Acuse:
    evento_uuid: str
    evento_id: str
    estado: str
    duplicado: bool
    recibido_en: datetime


class Receptor:
    def __init__(
        self,
        repositorio: RepositorioEventos,
        publicador: PublicadorEventos,
        reloj: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repositorio
        self._publicador = publicador
        self._reloj = reloj or (lambda: datetime.now(UTC))
        self._candados: dict[str, asyncio.Lock] = {}

    async def registrar_cuerpo_invalido(self, fuente: str, detalles: list[dict[str, str]]) -> None:
        await self._registrar_rechazo(fuente, None, "EVENTO_INVALIDO", detalles)

    async def _registrar_rechazo(
        self, fuente: str, evento_id: str | None, codigo: str, detalles: list[dict[str, str]]
    ) -> None:
        # El rechazo deja registro (criterio 3); si no se puede escribir, se propaga el fallo.
        await asyncio.to_thread(
            self._repo.registrar_rechazo,
            fuente=fuente,
            evento_id=evento_id,
            codigo=codigo,
            detalles=detalles,
            recibido_en=self._reloj(),
            correlacion_id=correlacion_id.get(),
        )
        log.info("evento rechazado fuente=%s codigo=%s", fuente, codigo)

    async def recibir(self, cuerpo: Any, fuente: str) -> Acuse:
        ahora = self._reloj()
        evento, defectos, evento_id = validar(cuerpo, fuente, ahora)
        if evento is None:
            detalles = [d.como_dict() for d in defectos]
            codigo = (
                "FUENTE_DESCONOCIDA"
                if any(d.campo == "fuente" for d in defectos)
                else "EVENTO_INVALIDO"
            )
            await self._registrar_rechazo(fuente, evento_id, codigo, detalles)
            raise EventoInvalido(detalles, codigo)

        guardado, nuevo = await asyncio.to_thread(
            self._repo.registrar_evento, evento, ahora, correlacion_id.get()
        )
        await self._publicar_una_vez(guardado)
        log.info(
            "evento recibido fuente=%s tipo=%s duplicado=%s", guardado.fuente, guardado.tipo, not nuevo
        )
        return Acuse(guardado.uuid, guardado.evento_id, "recibido", not nuevo, guardado.recibido_en)

    async def _publicar_una_vez(self, evento: EventoGuardado) -> None:
        """Publica si aún no se hizo; un reenvío de un evento registrado pero no publicado lo completa."""
        candado = self._candados.setdefault(evento.uuid, asyncio.Lock())
        try:
            async with candado:
                if evento.publicado or await asyncio.to_thread(self._repo.esta_publicado, evento.uuid):
                    return
                sobre = Evento(
                    event_id=evento.uuid,  # la reentrega se detecta por este id en el consumidor
                    event_type=TIPO_EVENTO,
                    source=FUENTE_SOBRE,
                    timestamp=evento.recibido_en,
                    correlation_id=evento.correlacion_id or evento.uuid,
                    payload=evento.mensaje(),
                )
                try:
                    await self._publicador.publicar(TOPIC_EVENTOS, sobre, clave=evento.clave_particion)
                except (KafkaError, TimeoutError, OSError) as exc:
                    log.warning("no se pudo publicar evento_id=%s: %s", evento.evento_id, type(exc).__name__)
                    raise BusNoDisponible from exc
                await asyncio.to_thread(
                    self._repo.registrar_publicacion, evento.uuid, TOPIC_EVENTOS, self._reloj()
                )
        finally:
            self._candados.pop(evento.uuid, None)

    async def resumen(self) -> dict[str, int]:
        return await asyncio.to_thread(self._repo.resumen, GRUPO_RECEPCION)


async def procesar_evento(evento: Evento, sesion: Session) -> None:
    """Manejador del consumidor de este sprint: la constancia de procesado la deja la reserva de idempotencia.

    HU28 agrega aquí (o en otro grupo `solventa-siniestros.*`) la evaluación de la condición de cada
    póliza, escribiendo con `sesion` para que se confirme junto con la reserva.
    """
