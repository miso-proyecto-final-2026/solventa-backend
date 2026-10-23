"""Recepción de eventos paramétricos (HU27): validar, registrar, publicar y acusar recibo.

El acuse sale en cuanto el evento está registrado y publicado en el bus; el procesamiento
(HU28) ocurre después y no lo espera.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.contexto import correlacion_id
from app.siniestros.bus import TOPIC_EVENTOS, BusEventos, BusSaturado, Mensaje
from app.siniestros.dominio import validar
from app.siniestros.errores import BusNoDisponible, EventoInvalido
from app.siniestros.repositorio import EventoGuardado, RepositorioEventos

log = logging.getLogger(__name__)

CONSUMIDOR_RECEPCION = "recepcion"


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
        bus: BusEventos,
        reloj: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repositorio
        self._bus = bus
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
                try:
                    mensaje = await self._bus.publicar(
                        TOPIC_EVENTOS, evento.clave_particion, evento.mensaje()
                    )
                except BusSaturado as exc:
                    raise BusNoDisponible from exc
                await asyncio.to_thread(
                    self._repo.registrar_publicacion,
                    evento.uuid,
                    mensaje.topic,
                    mensaje.particion,
                    mensaje.posicion,
                    self._reloj(),
                )
        finally:
            self._candados.pop(evento.uuid, None)

    async def reponer_rezago(self) -> int:
        """Vuelve a poner en el bus los eventos registrados que nadie procesó.

        El bus en memoria se pierde al reiniciar el proceso; la base conserva la verdad. Con el
        broker real (que persiste) esto no hace falta.
        """
        pendientes = await asyncio.to_thread(self._repo.sin_procesar, CONSUMIDOR_RECEPCION)
        for evento in pendientes:
            await self._bus.publicar(TOPIC_EVENTOS, evento.clave_particion, evento.mensaje())
        return len(pendientes)

    async def resumen(self) -> dict[str, int]:
        datos = await asyncio.to_thread(self._repo.resumen, CONSUMIDOR_RECEPCION)
        datos["rezago"] = await self._bus.rezago(TOPIC_EVENTOS, CONSUMIDOR_RECEPCION)
        return datos


class RegistradorProcesamiento:
    """Consumidor idempotente de este sprint: deja constancia de que cada evento llegó al final.

    HU28 agrega el consumidor que evalúa la condición de las pólizas con su propio grupo; este
    sirve de base para medir «enviados, aceptados y procesados» en la prueba de ráfaga.
    """

    def __init__(self, repositorio: RepositorioEventos, reloj: Callable[[], datetime] | None = None):
        self._repo = repositorio
        self._reloj = reloj or (lambda: datetime.now(UTC))

    async def __call__(self, mensaje: Mensaje) -> None:
        await asyncio.to_thread(
            self._repo.registrar_procesado,
            mensaje.valor["evento_uuid"],
            CONSUMIDOR_RECEPCION,
            self._reloj(),
        )
