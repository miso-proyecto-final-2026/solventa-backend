"""Bus de eventos de MS Siniestros detrás de una interfaz (ADR-0002: Kafka/MSK en el destino).

`BusMemoria` es el sustituto de este sprint: registro por partición con consumidores por grupo,
confirmación explícita (entrega al menos una vez) y contrapresión por rezago. Cuando el MSK esté
disponible se agrega otro adaptador con la misma interfaz; ni el servicio ni los consumidores cambian.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

TOPIC_EVENTOS = "solventa.siniestros.eventos"
TOPIC_EVENTOS_FALLIDOS = "solventa.siniestros.eventos.dlq"


class BusSaturado(Exception):
    """El rezago superó la capacidad: el productor debe reintentar luego (contrapresión)."""


@dataclass(frozen=True)
class Mensaje:
    topic: str
    particion: int
    posicion: int
    clave: str
    valor: dict[str, Any]


class BusEventos(Protocol):
    async def publicar(self, topic: str, clave: str, valor: dict[str, Any]) -> Mensaje: ...

    async def leer(self, topic: str, grupo: str, maximo: int) -> list[Mensaje]:
        """Mensajes aún sin confirmar del grupo, en orden dentro de cada partición."""
        ...

    async def confirmar(self, grupo: str, mensaje: Mensaje) -> None: ...

    async def rezago(self, topic: str, grupo: str) -> int: ...


def particion_de(clave: str, particiones: int) -> int:
    """Estable entre procesos (hash() de Python se aleatoriza por proceso)."""
    return zlib.crc32(clave.encode()) % particiones


class BusMemoria:
    def __init__(self, particiones: int = 6, capacidad_rezago: int = 200_000) -> None:
        self._particiones = particiones
        self._capacidad = capacidad_rezago
        self._registro: dict[str, list[list[Mensaje]]] = {}
        # (grupo, topic) → siguiente posición por confirmar de cada partición
        self._confirmado: dict[tuple[str, str], list[int]] = {}

    def _topic(self, topic: str) -> list[list[Mensaje]]:
        return self._registro.setdefault(topic, [[] for _ in range(self._particiones)])

    def _grupo(self, topic: str, grupo: str) -> list[int]:
        return self._confirmado.setdefault((grupo, topic), [0] * self._particiones)

    def registrar_grupo(self, topic: str, grupo: str) -> None:
        """Un grupo registrado cuenta para la contrapresión aunque todavía no haya leído."""
        self._topic(topic)
        self._grupo(topic, grupo)

    async def publicar(self, topic: str, clave: str, valor: dict[str, Any]) -> Mensaje:
        registro = self._topic(topic)
        grupos = [g for (g, t) in self._confirmado if t == topic]
        if grupos and max(self._rezago(topic, g) for g in grupos) >= self._capacidad:
            raise BusSaturado(topic)
        particion = particion_de(clave, self._particiones)
        mensaje = Mensaje(topic, particion, len(registro[particion]), clave, valor)
        registro[particion].append(mensaje)
        return mensaje

    async def leer(self, topic: str, grupo: str, maximo: int) -> list[Mensaje]:
        registro = self._topic(topic)
        confirmado = self._grupo(topic, grupo)
        pendientes: list[Mensaje] = []
        for particion, mensajes in enumerate(registro):
            pendientes.extend(mensajes[confirmado[particion] : confirmado[particion] + maximo])
            if len(pendientes) >= maximo:
                break
        return pendientes[:maximo]

    async def confirmar(self, grupo: str, mensaje: Mensaje) -> None:
        confirmado = self._grupo(mensaje.topic, grupo)
        confirmado[mensaje.particion] = max(confirmado[mensaje.particion], mensaje.posicion + 1)

    def _rezago(self, topic: str, grupo: str) -> int:
        registro = self._topic(topic)
        confirmado = self._grupo(topic, grupo)
        return sum(len(m) - confirmado[i] for i, m in enumerate(registro))

    async def rezago(self, topic: str, grupo: str) -> int:
        return self._rezago(topic, grupo)


Manejador = Callable[[Mensaje], Awaitable[None]]


class Consumidor:
    """Consume un topic en orden por partición, con reintentos de retroceso exponencial.

    Agotados los reintentos, el mensaje va a la cola de mensajes fallidos y se confirma: un
    mensaje venenoso no detiene el resto de la partición. Si el proceso cae, lo no confirmado
    se vuelve a entregar al volver (al menos una vez), de modo que el manejador debe ser idempotente.
    """

    def __init__(
        self,
        bus: BusEventos,
        topic: str,
        grupo: str,
        manejador: Manejador,
        *,
        reintentos: int = 3,
        espera_base_s: float = 0.2,
        topic_fallidos: str = TOPIC_EVENTOS_FALLIDOS,
        lote: int = 500,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._grupo = grupo
        self._manejador = manejador
        self._reintentos = reintentos
        self._espera_base_s = espera_base_s
        self._topic_fallidos = topic_fallidos
        self._lote = lote
        self._dormir = dormir

    async def procesar_lote(self) -> int:
        """Procesa hasta `lote` mensajes pendientes; devuelve cuántos confirmó."""
        mensajes = await self._bus.leer(self._topic, self._grupo, self._lote)
        for mensaje in mensajes:
            await self._procesar(mensaje)
        return len(mensajes)

    async def drenar(self) -> int:
        total = 0
        while (n := await self.procesar_lote()) > 0:
            total += n
        return total

    async def ejecutar(self, pausa_ociosa_s: float = 0.05) -> None:
        while True:
            if await self.procesar_lote() == 0:
                await self._dormir(pausa_ociosa_s)

    async def _procesar(self, mensaje: Mensaje) -> None:
        for intento in range(self._reintentos):
            try:
                await self._manejador(mensaje)
                break
            except Exception:  # noqa: BLE001  (cualquier fallo del manejador se reintenta)
                if intento + 1 < self._reintentos:
                    await self._dormir(self._espera_base_s * 2**intento)
        else:
            await self._bus.publicar(
                self._topic_fallidos, mensaje.clave, {**mensaje.valor, "_origen": self._topic}
            )
        await self._bus.confirmar(self._grupo, mensaje)
