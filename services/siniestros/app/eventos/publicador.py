"""Publicación de eventos: sobre estándar, clave de partición obligatoria y confirmación de todas las réplicas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, Self

from aiokafka import AIOKafkaProducer

from .sobre import Evento


class PublicadorEventos(Protocol):
    async def publicar(self, topic: str, evento: Evento, clave: str) -> None: ...

    async def publicar_crudo(self, topic: str, clave: str, valor: bytes, cabeceras: Mapping[str, str]) -> None: ...


def _exigir_clave(clave: str) -> bytes:
    if not clave:
        raise ValueError("Todo evento lleva clave de partición (documentación técnica §4.2): sin clave no se publica")
    return clave.encode()


class PublicadorKafka:
    """Productor idempotente con `acks=all`. Úselo como contexto: `async with PublicadorKafka(...) as publicador`."""

    def __init__(self, brokers: list[str], seguridad: Mapping[str, Any], client_id: str) -> None:
        self._productor = AIOKafkaProducer(
            bootstrap_servers=brokers,
            client_id=client_id,
            acks="all",
            enable_idempotence=True,
            **seguridad,
        )

    async def iniciar(self) -> None:
        await self._productor.start()

    async def detener(self) -> None:
        await self._productor.stop()

    async def __aenter__(self) -> Self:
        await self.iniciar()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.detener()

    async def publicar(self, topic: str, evento: Evento, clave: str) -> None:
        await self.publicar_crudo(
            topic,
            clave,
            evento.a_bytes(),
            {"event_type": evento.event_type, "correlation_id": evento.correlation_id},
        )

    async def publicar_crudo(self, topic: str, clave: str, valor: bytes, cabeceras: Mapping[str, str]) -> None:
        await self._productor.send_and_wait(
            topic,
            valor,
            key=_exigir_clave(clave),
            headers=[(k, v.encode()) for k, v in cabeceras.items()],
        )


class PublicadorMemoria:
    """Para pruebas unitarias del servicio: guarda lo publicado en lugar de enviarlo."""

    def __init__(self) -> None:
        self.publicados: list[tuple[str, str, Evento]] = []
        self.crudos: list[tuple[str, str, bytes, dict[str, str]]] = []

    async def publicar(self, topic: str, evento: Evento, clave: str) -> None:
        _exigir_clave(clave)
        self.publicados.append((topic, clave, evento))

    async def publicar_crudo(self, topic: str, clave: str, valor: bytes, cabeceras: Mapping[str, str]) -> None:
        _exigir_clave(clave)
        self.crudos.append((topic, clave, valor, dict(cabeceras)))
