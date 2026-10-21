"""Token de acceso del personal interno (HU38).

Es provisional: Identidad firma un JWT HS256 de vida corta con el rol del usuario. El puerto
`EmisorToken` permite cambiarlo por OIDC sin tocar los endpoints. Cotización valida el mismo
token con su propio verificador (no se comparte código entre servicios, ADR-0001).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

import jwt

EMISOR = "solventa-identidad"
TTL_POR_DEFECTO_MIN = 30
_VERDADERO = {"1", "true", "yes", "si"}
_SECRETO_DESARROLLO = "solo-desarrollo-local-no-usar-en-produccion"


class ConfiguracionTokenInvalida(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimsInternos:
    usuario_id: str
    rol: str
    nombre: str


class EmisorToken(Protocol):
    ttl_s: int

    def emitir(self, usuario_id: str, rol: str, nombre: str) -> str: ...

    def verificar(self, token: str) -> ClaimsInternos | None: ...


class EmisorJwtHs256:
    def __init__(self, secreto: str, ttl_min: int = TTL_POR_DEFECTO_MIN) -> None:
        if not secreto:
            raise ConfiguracionTokenInvalida("Falta el secreto para firmar el token interno")
        self._secreto = secreto
        self.ttl_s = ttl_min * 60

    def emitir(self, usuario_id: str, rol: str, nombre: str) -> str:
        ahora = int(time.time())
        claims = {
            "sub": usuario_id,
            "rol": rol,
            "nombre": nombre,
            "iat": ahora,
            "exp": ahora + self.ttl_s,
            "iss": EMISOR,
        }
        return jwt.encode(claims, self._secreto, algorithm="HS256")

    def verificar(self, token: str) -> ClaimsInternos | None:
        """Devuelve los claims si el token es válido y vigente; None en cualquier otro caso."""
        try:
            c = jwt.decode(
                token,
                self._secreto,
                algorithms=["HS256"],
                issuer=EMISOR,
                options={"require": ["exp", "iss", "sub", "rol"]},
            )
        except jwt.PyJWTError:
            return None
        return ClaimsInternos(c["sub"], c["rol"], c.get("nombre", ""))


def emisor_desde_entorno(env: dict[str, str] | None = None) -> EmisorToken:
    """Falla cerrado: sin `ASESOR_JWT_SECRETO` solo se acepta un secreto fijo en modo desarrollo."""
    e = os.environ if env is None else env
    secreto = e.get("ASESOR_JWT_SECRETO", "").strip()
    if not secreto:
        if e.get("SOLVENTA_MODO_DESARROLLO", "").strip().lower() in _VERDADERO:
            secreto = _SECRETO_DESARROLLO
        else:
            raise ConfiguracionTokenInvalida(
                "ASESOR_JWT_SECRETO no está definido. Defínalo, o use SOLVENTA_MODO_DESARROLLO=1 "
                "solo en desarrollo local."
            )
    return EmisorJwtHs256(secreto, int(e.get("ASESOR_JWT_TTL_MIN", TTL_POR_DEFECTO_MIN)))
