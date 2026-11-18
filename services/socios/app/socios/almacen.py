"""Almacén efímero de la cuota y la idempotencia (ADR-0025): Redis en staging, memoria en desarrollo.

Ambos son estado técnico con TTL, compartido entre réplicas a través de Redis. En memoria cada réplica tiene el suyo,
lo que solo se acepta con SOLVENTA_MODO_DESARROLLO (ver config.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol


class ErrorAlmacen(Exception):
    """El almacén no respondió. Quien llama decide si falla abierto (cuota) o cerrado (idempotencia)."""


class Almacen(Protocol):
    async def incrementar(self, clave: str, ttl_s: int) -> int: ...

    async def crear_si_no_existe(self, clave: str, valor: str, ttl_s: int) -> bool: ...

    async def leer(self, clave: str) -> str | None: ...

    async def escribir(self, clave: str, valor: str, ttl_s: int) -> None: ...

    async def borrar(self, clave: str) -> None: ...

    async def disponible(self) -> bool: ...

    async def cerrar(self) -> None: ...


class AlmacenRedis:
    def __init__(self, cliente: Any) -> None:
        self._r = cliente

    @classmethod
    def desde_url(cls, url: str) -> AlmacenRedis:
        import redis.asyncio as redis

        # Timeouts cortos: Redis está en la ruta de cada cotización (HA01).
        return cls(redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.5, decode_responses=True))

    async def incrementar(self, clave: str, ttl_s: int) -> int:
        try:
            async with self._r.pipeline(transaction=True) as tubo:
                tubo.incr(clave)
                tubo.expire(clave, ttl_s)
                valor, _ = await tubo.execute()
            return int(valor)
        except Exception as error:
            raise ErrorAlmacen(type(error).__name__) from error

    async def crear_si_no_existe(self, clave: str, valor: str, ttl_s: int) -> bool:
        try:
            return bool(await self._r.set(clave, valor, ex=ttl_s, nx=True))
        except Exception as error:
            raise ErrorAlmacen(type(error).__name__) from error

    async def leer(self, clave: str) -> str | None:
        try:
            return await self._r.get(clave)
        except Exception as error:
            raise ErrorAlmacen(type(error).__name__) from error

    async def escribir(self, clave: str, valor: str, ttl_s: int) -> None:
        try:
            await self._r.set(clave, valor, ex=ttl_s)
        except Exception as error:
            raise ErrorAlmacen(type(error).__name__) from error

    async def borrar(self, clave: str) -> None:
        try:
            await self._r.delete(clave)
        except Exception as error:
            raise ErrorAlmacen(type(error).__name__) from error

    async def disponible(self) -> bool:
        try:
            return bool(await self._r.ping())
        except Exception:  # noqa: BLE001
            return False

    async def cerrar(self) -> None:
        await self._r.aclose()


class AlmacenMemoria:
    """Sustituto local: un diccionario con vencimiento. Solo para desarrollo y pruebas."""

    def __init__(self, reloj: Callable[[], float] = time.monotonic) -> None:
        self._datos: dict[str, tuple[str, float]] = {}
        self._reloj = reloj

    def _vigente(self, clave: str) -> str | None:
        entrada = self._datos.get(clave)
        if entrada is None:
            return None
        valor, vence = entrada
        if vence <= self._reloj():
            del self._datos[clave]
            return None
        return valor

    async def incrementar(self, clave: str, ttl_s: int) -> int:
        valor = int(self._vigente(clave) or 0) + 1
        self._datos[clave] = (str(valor), self._reloj() + ttl_s)
        return valor

    async def crear_si_no_existe(self, clave: str, valor: str, ttl_s: int) -> bool:
        if self._vigente(clave) is not None:
            return False
        self._datos[clave] = (valor, self._reloj() + ttl_s)
        return True

    async def leer(self, clave: str) -> str | None:
        return self._vigente(clave)

    async def escribir(self, clave: str, valor: str, ttl_s: int) -> None:
        self._datos[clave] = (valor, self._reloj() + ttl_s)

    async def borrar(self, clave: str) -> None:
        self._datos.pop(clave, None)

    async def disponible(self) -> bool:
        return True

    async def cerrar(self) -> None:
        self._datos.clear()
