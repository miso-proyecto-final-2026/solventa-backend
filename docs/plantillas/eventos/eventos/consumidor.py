"""Consumidor idempotente con reintentos y cola de mensajes fallidos (ADR-0002: entrega al menos una vez).

Por cada mensaje, en una sola transacción de base de datos:
  1. se reserva la clave de idempotencia (`event_id` por consumidor): si ya estaba, es una reentrega y se omite;
  2. se ejecuta el manejador con la misma sesión: si falla, la reserva y todo lo que escribió se deshacen.
Ante un fallo se reintenta con retroceso exponencial; agotados los intentos, el mensaje va a `<topic>.dlq`.
El offset solo se confirma cuando el mensaje terminó (procesado, omitido o en la cola de fallidos): si la cola de
fallidos también falla, no se confirma y el mensaje se vuelve a entregar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aiokafka import AIOKafkaConsumer, ConsumerRecord
from sqlalchemy.orm import Session

from .publicador import PublicadorEventos
from .registro import RegistroEventos
from .sobre import Evento, EventoInvalido

log = logging.getLogger("solventa.eventos")

Manejador = Callable[[Evento, Session], Awaitable[None]]


@dataclass(frozen=True)
class PoliticaReintentos:
    """Retroceso exponencial: espera `base_s * 2^(n-1)` entre intentos, con tope `maximo_s`."""

    intentos: int = 5
    base_s: float = 1.0
    maximo_s: float = 30.0

    def __post_init__(self) -> None:
        if self.intentos < 1 or self.base_s <= 0 or self.maximo_s <= 0:
            raise ValueError("intentos, base_s y maximo_s deben ser positivos")

    def espera(self, intento: int) -> float:
        return min(self.maximo_s, self.base_s * 2 ** (intento - 1))


class ConsumidorEventos:
    def __init__(
        self,
        *,
        brokers: list[str],
        seguridad: Mapping[str, Any],
        topic: str,
        grupo: str,
        manejador: Manejador,
        fabrica_sesiones: Callable[[], Session],
        registro: RegistroEventos,
        publicador: PublicadorEventos,
        politica: PoliticaReintentos | None = None,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._topic = topic
        self._grupo = grupo
        self._manejador = manejador
        self._fabrica = fabrica_sesiones
        self._registro = registro
        self._publicador = publicador
        self._politica = politica or PoliticaReintentos()
        self._dormir = dormir
        self._detenido = asyncio.Event()
        self._consumidor = AIOKafkaConsumer(
            topic,
            bootstrap_servers=brokers,
            group_id=grupo,
            client_id=f"{grupo}-consumidor",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            **seguridad,
        )

    async def iniciar(self) -> None:
        await self._consumidor.start()

    async def detener(self) -> None:
        self._detenido.set()
        await self._consumidor.stop()

    async def ejecutar(self) -> None:
        """Procesa mensajes hasta que se llame a `detener()`."""
        self._detenido.clear()
        while not self._detenido.is_set():
            lote = await self._consumidor.getmany(timeout_ms=500, max_records=10)
            for particion, mensajes in lote.items():
                for mensaje in mensajes:
                    await self.procesar(mensaje)
                    await self._consumidor.commit({particion: mensaje.offset + 1})

    async def procesar(self, mensaje: ConsumerRecord) -> None:
        try:
            evento = Evento.desde_bytes(mensaje.value)
        except EventoInvalido as error:
            # Un mensaje ilegible no mejora al reintentarlo: directo a la cola de fallidos.
            await self._a_cola_de_fallidos(mensaje, f"EventoInvalido: {error}", intentos=0)
            return

        ultimo_error: Exception | None = None
        for intento in range(1, self._politica.intentos + 1):
            try:
                if await self._intentar(evento):
                    return
                log.info("reentrega omitida event_id=%s grupo=%s", evento.event_id, self._grupo)
                return
            except Exception as error:  # noqa: BLE001  (cualquier fallo del manejador se reintenta)
                ultimo_error = error
                log.warning("fallo event_id=%s intento=%d/%d: %s", evento.event_id, intento, self._politica.intentos, error)
                if intento < self._politica.intentos:
                    await self._dormir(self._politica.espera(intento))
        await self._a_cola_de_fallidos(mensaje, f"{type(ultimo_error).__name__}: {ultimo_error}", self._politica.intentos)

    async def _intentar(self, evento: Evento) -> bool:
        """True si lo procesó; False si era una reentrega. Todo ocurre en una transacción."""
        with self._fabrica() as sesion, sesion.begin():
            if not self._registro.reservar(sesion, self._grupo, evento.event_id):
                return False
            await self._manejador(evento, sesion)
            return True

    async def _a_cola_de_fallidos(self, mensaje: ConsumerRecord, error: str, intentos: int) -> None:
        clave = mensaje.key.decode() if mensaje.key else "sin-clave"
        await self._publicador.publicar_crudo(
            f"{self._topic}.dlq",
            clave,
            mensaje.value,
            {
                "error": error[:500],
                "intentos": str(intentos),
                "grupo": self._grupo,
                "topic_origen": self._topic,
                "particion": str(mensaje.partition),
                "offset": str(mensaje.offset),
            },
        )
        log.error("evento enviado a %s.dlq grupo=%s offset=%d: %s", self._topic, self._grupo, mensaje.offset, error)
