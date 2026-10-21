"""Caché de perfiles (cache-aside) sobre Redis, con respaldo para degradación.

Clave: ``perfil:{cliente_id}:{fuente}``. Además del valor vigente (TTL con jitter)
se guarda una copia de respaldo de larga duración para servir el "último perfil
cacheado" cuando el proveedor degrada.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from app.proveedores.dominio import Fuente


class CachePerfil(Protocol):
    async def obtener(
        self, cliente_id: str, fuente: Fuente
    ) -> dict[str, Any] | None: ...

    async def obtener_respaldo(
        self, cliente_id: str, fuente: Fuente
    ) -> dict[str, Any] | None: ...

    async def guardar(
        self,
        cliente_id: str,
        fuente: Fuente,
        payload: dict[str, Any],
        ttl_s: int,
        ttl_respaldo_s: int,
    ) -> None: ...


def _clave(cliente_id: str, fuente: Fuente) -> str:
    return f"perfil:{cliente_id}:{fuente.value}"


class CacheRedis:
    def __init__(self, cliente_redis: Any) -> None:
        self._r = cliente_redis

    async def _leer(self, clave: str) -> dict[str, Any] | None:
        crudo = await self._r.get(clave)
        return None if crudo is None else json.loads(crudo)

    async def obtener(self, cliente_id: str, fuente: Fuente) -> dict[str, Any] | None:
        return await self._leer(_clave(cliente_id, fuente))

    async def obtener_respaldo(
        self, cliente_id: str, fuente: Fuente
    ) -> dict[str, Any] | None:
        return await self._leer(_clave(cliente_id, fuente) + ":respaldo")

    async def guardar(
        self,
        cliente_id: str,
        fuente: Fuente,
        payload: dict[str, Any],
        ttl_s: int,
        ttl_respaldo_s: int,
    ) -> None:
        clave = _clave(cliente_id, fuente)
        valor = json.dumps(payload)
        await self._r.set(clave, valor, ex=ttl_s)
        await self._r.set(clave + ":respaldo", valor, ex=ttl_respaldo_s)


class CacheMemoria:
    """Respaldo para desarrollo local sin Redis (sin expiración)."""

    def __init__(self) -> None:
        self._datos: dict[str, dict[str, Any]] = {}

    async def obtener(self, cliente_id: str, fuente: Fuente) -> dict[str, Any] | None:
        return self._datos.get(_clave(cliente_id, fuente))

    async def obtener_respaldo(
        self, cliente_id: str, fuente: Fuente
    ) -> dict[str, Any] | None:
        return self._datos.get(_clave(cliente_id, fuente))

    async def guardar(
        self,
        cliente_id: str,
        fuente: Fuente,
        payload: dict[str, Any],
        ttl_s: int,
        ttl_respaldo_s: int,
    ) -> None:
        self._datos[_clave(cliente_id, fuente)] = payload
