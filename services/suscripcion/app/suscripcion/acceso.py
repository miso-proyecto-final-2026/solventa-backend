"""Acceso del asesor a la venta asistida (HU39).

Mismo mecanismo provisional que Cotización y Pagos (ADR-0008): Identidad emite un JWT HS256 de vida corta con el rol y aquí se
valida con el mismo secreto. El verificador se duplica a propósito (ADR-0001: no se comparte código entre servicios) y el puerto
`VerificadorToken` permite pasar a OIDC/JWKS sin tocar los endpoints. Solo protege `/v1/suscripciones/asistidas`: las demás rutas
siguen con el token del cliente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol

import jwt
from fastapi import Depends, Header, Request

from app.configuracion import permitir_sustituto_local
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.errores import AccesoDenegado, NoAutenticado

ROL_ASESOR = "ASESOR"
_SECRETO_DESARROLLO = "solo-desarrollo-local-no-usar-en-produccion"


@dataclass(frozen=True)
class ClaimsAcceso:
    usuario_id: str
    rol: str
    nombre: str


@dataclass(frozen=True)
class Asesor:
    id: str
    nombre: str


class VerificadorToken(Protocol):
    def verificar(self, token: str) -> ClaimsAcceso | None:
        """Claims si el token es válido y vigente; None en cualquier otro caso."""
        ...


class VerificadorJwtHs256:
    def __init__(self, secreto: str, emisor: str) -> None:
        self._secreto = secreto
        self._emisor = emisor

    def verificar(self, token: str) -> ClaimsAcceso | None:
        try:
            c = jwt.decode(
                token,
                self._secreto,
                algorithms=["HS256"],
                issuer=self._emisor,
                options={"require": ["exp", "iss", "sub", "rol"]},
            )
        except jwt.PyJWTError:
            return None
        return ClaimsAcceso(c["sub"], c["rol"], c.get("nombre", ""))


def construir_verificador(config: ConfigSuscripcion) -> VerificadorToken:
    """Falla cerrado: sin secreto solo arranca en modo desarrollo."""
    secreto = config.asesor_jwt_secreto
    if not secreto:
        permitir_sustituto_local("ASESOR_JWT_SECRETO", "un secreto fijo de desarrollo")
        secreto = _SECRETO_DESARROLLO
    return VerificadorJwtHs256(secreto, config.asesor_jwt_emisor)


def exigir_asesor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Asesor:
    """401 si falta el token o no es válido (el portal lo trata como sesión vencida); 403 si no es asesor."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise NoAutenticado("Se requiere iniciar sesión.")
    verificador: VerificadorToken = request.app.state.verificador_asesor
    claims = verificador.verificar(authorization[7:].strip())
    if claims is None:
        raise NoAutenticado("La sesión no es válida o venció.")
    if claims.rol != ROL_ASESOR:
        raise AccesoDenegado("Se requiere el rol de asesor.")
    return Asesor(claims.usuario_id, claims.nombre)


AsesorActual = Annotated[Asesor, Depends(exigir_asesor)]
