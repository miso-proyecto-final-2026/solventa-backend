"""Socios y contratos: autenticación del `client_secret` y contrato vigente con caché corta (HU32, criterios 2 y 4).

El contrato se consulta en cada solicitud, no se confía en el `scope` del token: así deshabilitar un endpoint o un
socio surte efecto en `ttl_contratos_s` (60 s) y no al vencer el token (1 h). La caché evita un viaje a la base por
solicitud en la ruta de la cotización (HA01).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.socios import repositorio as repo
from app.socios.modelos_db import ESTADO_ACTIVO
from app.socios.secretos import hash_ficticio, verificar_secreto

VERSION_V1 = "v1"


@dataclass(frozen=True)
class Contrato:
    version: str
    endpoints: frozenset[str]
    cuota_rps: int

    def habilita(self, endpoint: str) -> bool:
        return endpoint in self.endpoints


def _ahora() -> datetime:
    return datetime.now(UTC)


class RegistroSocios:
    def __init__(
        self,
        fabrica: sessionmaker[Session],
        ttl_contratos_s: float = 60.0,
        reloj: Callable[[], datetime] = _ahora,
        monotono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fabrica = fabrica
        self._ttl = ttl_contratos_s
        self._reloj = reloj
        self._monotono = monotono
        self._cache: dict[tuple[str, str], tuple[Contrato | None, float]] = {}

    def _autenticar(self, client_id: str, client_secret: str) -> Contrato | None:
        with self._fabrica() as sesion:
            cliente = repo.buscar_cliente(sesion, client_id)
            hash_guardado = cliente.client_secret_hash if cliente is not None else hash_ficticio()
            valido = verificar_secreto(client_secret, hash_guardado)
            if cliente is None or not valido or cliente.estado != ESTADO_ACTIVO:
                return None
            return self._contrato_sincrono(sesion, client_id, VERSION_V1) or Contrato(VERSION_V1, frozenset(), 0)

    async def autenticar(self, client_id: str, client_secret: str) -> Contrato | None:
        """El contrato v1 si las credenciales son válidas y el socio está activo; None en cualquier otro caso.

        Un socio activo sin contrato vigente recibe token, pero sin endpoints habilitados (todo responde 403).
        """
        return await asyncio.to_thread(self._autenticar, client_id, client_secret)

    def _contrato_sincrono(self, sesion: Session, client_id: str, version: str) -> Contrato | None:
        fila = repo.contrato_vigente(sesion, client_id, version, self._reloj())
        if fila is None:
            return None
        return Contrato(fila.version, frozenset(str(e) for e in fila.endpoints_habilitados), int(fila.cuota_rps))

    def _consultar(self, client_id: str, version: str) -> Contrato | None:
        with self._fabrica() as sesion:
            return self._contrato_sincrono(sesion, client_id, version)

    async def contrato(self, client_id: str, version: str = VERSION_V1) -> Contrato | None:
        llave = (client_id, version)
        guardado = self._cache.get(llave)
        if guardado is not None and guardado[1] > self._monotono():
            return guardado[0]
        contrato = await asyncio.to_thread(self._consultar, client_id, version)
        self._cache[llave] = (contrato, self._monotono() + self._ttl)
        return contrato

    def _bd_disponible(self) -> bool:
        try:
            with self._fabrica() as sesion:
                sesion.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001  (cualquier fallo de conexión)
            return False

    async def bd_disponible(self) -> bool:
        return await asyncio.to_thread(self._bd_disponible)
