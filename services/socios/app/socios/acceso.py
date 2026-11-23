"""Acceso a `/partners/v1`: token válido → contrato que habilita el endpoint → cuota (HU32, criterios 3, 4 y 5).

El orden importa: una solicitud sin token o sin permiso no consume cuota del socio.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request

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
ESQUEMA_BEARER = "bearerAuth"
_RUTA_PUBLICA = "/partners/v1/status"


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


def publicar_seguridad_bearer(app: FastAPI) -> None:
    """Declara en el OpenAPI el `Bearer` que ya exige `exigir_endpoint` (HA07): solo documenta, no cambia el acceso.

    La validación del token es manual (`_token`), así que FastAPI no la ve; sin esto, un integrador que solo lee el
    contrato no sabe que debe enviar `Authorization: Bearer <token>`.
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            esquema = original()
            esquema.setdefault("components", {}).setdefault("securitySchemes", {})[ESQUEMA_BEARER] = {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Token de `POST /oauth/token` (OAuth2 client credentials); su vigencia, en segundos, viene en "
                    "`expires_in`. Se envía como `Authorization: Bearer <token>`. Lo que el socio puede llamar lo decide "
                    "su contrato API, no el token: un endpoint no habilitado responde 403 `ENDPOINT_NOT_AUTHORIZED`."
                ),
            }
            for ruta, metodos in esquema["paths"].items():
                if ruta.startswith("/partners/") and ruta != _RUTA_PUBLICA:
                    for operacion in metodos.values():
                        operacion["security"] = [{ESQUEMA_BEARER: []}]
        return app.openapi_schema or esquema

    app.openapi = openapi  # type: ignore[method-assign]
