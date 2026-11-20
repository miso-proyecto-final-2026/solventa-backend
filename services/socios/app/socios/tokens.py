"""Tokens de acceso de los socios: JWT HS256 de 1 hora firmado por MS Socios (HU32, criterio 2; ADR-0025).

La validación no consulta la base ni Redis (sin estado): el costo en la ruta de la cotización es solo verificar la
firma (HA01). Qué puede hacer el socio lo decide su contrato vigente en cada solicitud, no el `scope` del token:
el `scope` informa lo habilitado al emitirlo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

EMISOR = "solventa-socios"
AUDIENCIA = "solventa-partners"
ALGORITMO = "HS256"


class ErrorToken(Exception):
    pass


class TokenExpiradoError(ErrorToken):
    pass


@dataclass(frozen=True)
class TokenEmitido:
    access_token: str
    expires_in: int
    scope: str


@dataclass(frozen=True)
class ClaimsSocio:
    client_id: str
    scope: tuple[str, ...]


class EmisorTokens:
    def __init__(self, secreto: str, ttl_s: int = 3600) -> None:
        self._secreto = secreto
        self._ttl_s = ttl_s

    def emitir(self, client_id: str, scopes: list[str], ahora: datetime) -> TokenEmitido:
        scope = " ".join(sorted(scopes))
        claims = {
            "iss": EMISOR,
            "aud": AUDIENCIA,
            "sub": client_id,
            "scope": scope,
            "iat": int(ahora.timestamp()),
            "exp": int((ahora + timedelta(seconds=self._ttl_s)).timestamp()),
        }
        return TokenEmitido(jwt.encode(claims, self._secreto, algorithm=ALGORITMO), self._ttl_s, scope)

    def verificar(self, token: str) -> ClaimsSocio:
        try:
            claims = jwt.decode(
                token,
                self._secreto,
                algorithms=[ALGORITMO],
                audience=AUDIENCIA,
                issuer=EMISOR,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as error:
            raise TokenExpiradoError from error
        except jwt.PyJWTError as error:
            raise ErrorToken(type(error).__name__) from error
        return ClaimsSocio(client_id=str(claims["sub"]), scope=tuple(str(claims.get("scope", "")).split()))
