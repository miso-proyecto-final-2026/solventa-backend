"""Publica los eventos guardados en `eventos_salida` (outbox). Al menos una vez: los consumidores son idempotentes."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.eventos import Evento, PublicadorEventos
from app.pagos import repositorio as repo

log = logging.getLogger("pagos.relay")


class RelayEventos:
    def __init__(self, fabrica: sessionmaker[Session], publicador: PublicadorEventos, lote: int = 50) -> None:
        self._fabrica = fabrica
        self._publicador = publicador
        self._lote = lote

    async def publicar_pendientes(self) -> int:
        """Publica un lote. Las filas se bloquean con SKIP LOCKED: dos réplicas no publican lo mismo a la vez."""
        publicados = 0
        with self._fabrica() as s, s.begin():
            for salida in repo.salidas_pendientes(s, limite=self._lote):
                try:
                    evento = Evento.model_validate(salida.payload)
                    await self._publicador.publicar(salida.topic, evento, salida.clave)
                except Exception as error:  # noqa: BLE001 - se reintenta en la próxima vuelta
                    repo.marcar_fallo(salida, f"{type(error).__name__}: {error}")
                    log.warning("no se pudo publicar event_id=%s: %s", salida.event_id, error)
                    break  # conserva el orden: no se adelanta un evento posterior
                repo.marcar_publicado(salida, ahora=datetime.now(UTC))
                publicados += 1
        return publicados

    async def ejecutar(self, intervalo_s: float = 0.5) -> None:
        while True:
            try:
                if await self.publicar_pendientes() == 0:
                    await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el relevo de eventos")
                await asyncio.sleep(intervalo_s * 4)
