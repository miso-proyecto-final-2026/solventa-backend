"""Escritura asíncrona y con reintento de la trazabilidad de consultas (HU11).

Fuera del camino crítico: `encolar` no bloquea ni lanza; un hilo de fondo escribe por
lotes. Si la escritura falla se reintenta con espera creciente y, agotados los
reintentos, se intenta fila a fila para aislar un registro inválido; lo que aun así
falla se pierde con un log de error (la alerta) y se cuenta.

Límite conocido: la cola vive en memoria. Si el proceso muere con registros
pendientes se pierden; una auditoría estricta requeriría una cola durable (Kafka).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.proveedores.dominio import RegistroTrazabilidad

log = logging.getLogger("cotizacion.trazabilidad")


class AlmacenTrazabilidad(Protocol):
    def insertar_lote(self, registros: list[RegistroTrazabilidad]) -> None: ...


class EscritorTrazabilidad:
    def __init__(
        self,
        almacen: AlmacenTrazabilidad,
        capacidad: int = 10_000,
        reintentos: int = 3,
        espera_s: float = 0.1,
        tamano_lote: int = 50,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._almacen = almacen
        self._cola: asyncio.Queue[RegistroTrazabilidad] = asyncio.Queue(capacidad)
        self._reintentos = reintentos
        self._espera_s = espera_s
        self._tamano_lote = tamano_lote
        self._dormir = dormir
        self._tarea: asyncio.Task[None] | None = None
        self.escritos = 0
        self.descartados = 0  # no cupieron en la cola
        self.perdidos = 0  # fallaron todos los reintentos

    def encolar(self, registro: RegistroTrazabilidad) -> None:
        """No bloquea ni lanza (criterio 6). Si la cola está llena, descarta y alerta."""
        try:
            self._cola.put_nowait(registro)
        except asyncio.QueueFull:
            self.descartados += 1
            log.error(
                "ALERTA cola de trazabilidad llena; registro descartado (total=%d)",
                self.descartados,
            )

    def iniciar(self) -> None:
        if self._tarea is None:
            self._tarea = asyncio.create_task(self._trabajar())

    async def detener(self) -> None:
        """Escribe lo pendiente y detiene el trabajador (apagado ordenado)."""
        await self.vaciar()
        if self._tarea is not None:
            self._tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarea
            self._tarea = None

    async def vaciar(self) -> None:
        """Espera a que todo lo encolado esté escrito (o contado como perdido)."""
        if self._tarea is None:
            self.iniciar()
        await self._cola.join()

    async def _trabajar(self) -> None:
        while True:
            lote = [await self._cola.get()]
            while len(lote) < self._tamano_lote and not self._cola.empty():
                lote.append(self._cola.get_nowait())
            try:
                await self._escribir(lote)
            except Exception:  # el trabajador nunca debe morir
                log.exception("ALERTA error inesperado escribiendo trazabilidad")
                self.perdidos += len(lote)
            finally:
                for _ in lote:
                    self._cola.task_done()

    async def _escribir(self, lote: list[RegistroTrazabilidad]) -> None:
        for intento in range(1, self._reintentos + 1):
            try:
                await asyncio.to_thread(self._almacen.insertar_lote, lote)
                self.escritos += len(lote)
                return
            except Exception as exc:  # noqa: BLE001  (cualquier fallo se reintenta)
                log.warning(
                    "fallo al escribir trazabilidad (intento %d/%d): %s",
                    intento,
                    self._reintentos,
                    type(exc).__name__,
                )
                if intento < self._reintentos:
                    await self._dormir(self._espera_s * 2 ** (intento - 1))
        # Se aísla un registro inválido para no perder el resto del lote.
        for registro in lote:
            try:
                await asyncio.to_thread(self._almacen.insertar_lote, [registro])
                self.escritos += 1
            except Exception as exc:  # noqa: BLE001  (se cuenta y se alerta)
                self.perdidos += 1
                log.error(
                    "ALERTA registro de trazabilidad perdido id=%s error=%s",
                    registro.id,
                    type(exc).__name__,
                )
