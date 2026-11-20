"""Acceso a `/partners/v1`: token válido → contrato que habilita el endpoint → cuota (HU32, criterios 3, 4 y 5).

El orden importa: una solicitud sin token o sin permiso no consume cuota del socio.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from app.socios.errores import (
    CuotaExcedida,
    EndpointNoAutorizado,
    TokenExpirado,
    TokenInvalido,
)
from app.socios.registro import Contrato
from app.socios.tokens import ErrorToken, TokenExpiradoError

ENDPOINT_COTIZACIONES = "cotizaciones:crear"
ENDPOINT_SUSCRIPCIONES_CREAR = "suscripciones:crear"
ENDPOINT_SUSCRIPCIONES_FIRMAR = "suscripciones:firmar"
ENDPOINT_POLIZAS_CONSULTAR = "polizas:consultar"
ENDPOINTS_CONOCIDOS = frozenset(
    {ENDPOINT_COTIZACIONES, ENDPOINT_SUSCRIPCIONES_CREAR, ENDPOINT_SUSCRIPCIONES_FIRMAR, ENDPOINT_POLIZAS_CONSULTAR}
)
_REALM = 'Bearer realm="solventa-partners"'


@dataclass(frozen=True)
class SocioAutorizado:
    client_id: str
    contrato: Contrato


def _token(request: Request) -> str:
    valor = request.headers.get("Authorization", "")
    esquema, _, token = valor.partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        raise TokenInvalido(cabeceras={"WWW-Authenticate": _REALM})
    return token.strip()


def exigir_endpoint(endpoint: str, version: str = "v1") -> Callable[[Request], Awaitable[SocioAutorizado]]:
    async def dependencia(request: Request) -> SocioAutorizado:
        estado = request.app.state
        try:
            claims = estado.tokens.verificar(_token(request))
        except TokenExpiradoError as error:
            raise TokenExpirado(cabeceras={"WWW-Authenticate": f'{_REALM}, error="invalid_token"'}) from error
        except ErrorToken as error:
            raise TokenInvalido(cabeceras={"WWW-Authenticate": f'{_REALM}, error="invalid_token"'}) from error

        contrato = await estado.registro.contrato(claims.client_id, version)
        if contrato is None or not contrato.habilita(endpoint):
            raise EndpointNoAutorizado

        reintentar_en = await estado.limitador.consumir(claims.client_id, contrato.cuota_rps)
        if reintentar_en is not None:
            raise CuotaExcedida(
                cabeceras={"Retry-After": str(reintentar_en), "X-RateLimit-Limit": str(contrato.cuota_rps)}
            )
        return SocioAutorizado(claims.client_id, contrato)

    return dependencia
