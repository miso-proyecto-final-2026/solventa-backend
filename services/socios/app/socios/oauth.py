"""`POST /oauth/token`: OAuth2 *client credentials* (RFC 6749 §4.4) para los socios (HU32, criterio 2).

Acepta las credenciales en el cuerpo (`client_id`, `client_secret`) o por HTTP Basic (RFC 6749 §2.3.1). Responde un
JWT de 1 hora. Los errores siguen RFC 6749 §5.2 (`{"error": "invalid_client", ...}`) y no el formato estándar de
`/partners/v1`: es lo que esperan las librerías OAuth2 de los socios. Se agrega `trace_id` (RFC 6749 permite campos
adicionales) para que el socio pueda reportar el caso.

Ni el secreto ni el token se registran en logs (criterio 11): solo el `client_id` y el resultado.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.contexto import traza_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

GRANT_CLIENT_CREDENTIALS = "client_credentials"
_SIN_CACHE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class TokenOut(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str


class ErrorOAuthOut(BaseModel):
    error: str
    error_description: str
    trace_id: str


def _error(status: int, codigo: str, descripcion: str, cabeceras: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": codigo, "error_description": descripcion, "trace_id": traza_id.get() or str(uuid.uuid4())},
        headers={**_SIN_CACHE, **(cabeceras or {})},
    )


def _credenciales_basic(request: Request) -> tuple[str, str] | None:
    valor = request.headers.get("Authorization", "")
    esquema, _, codificado = valor.partition(" ")
    if esquema.lower() != "basic" or not codificado:
        return None
    try:
        usuario, separador, clave = base64.b64decode(codificado, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return ("", "")
    if not separador:
        return ("", "")
    # RFC 6749 §2.3.1: el client_id y el secreto van codificados como application/x-www-form-urlencoded.
    return unquote(usuario), unquote(clave)


@router.post(
    "/token",
    response_model=TokenOut,
    summary="Obtener un token de acceso (OAuth2 client credentials)",
    responses={
        400: {"model": ErrorOAuthOut, "description": "Solicitud inválida o grant no soportado."},
        401: {"model": ErrorOAuthOut, "description": "Credenciales del cliente inválidas."},
    },
)
async def emitir_token(
    request: Request,
    grant_type: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    basic = _credenciales_basic(request)
    if grant_type is None:
        return _error(400, "invalid_request", "Falta grant_type.")
    if grant_type != GRANT_CLIENT_CREDENTIALS:
        return _error(400, "unsupported_grant_type", "Solo se admite grant_type=client_credentials.")
    if basic is not None and (client_id or client_secret):
        return _error(400, "invalid_request", "Envíe las credenciales en el cuerpo o por HTTP Basic, no en ambos.")
    if basic is not None:
        client_id, client_secret = basic
    if not client_id or not client_secret:
        return _error(400, "invalid_request", "Faltan client_id o client_secret.")

    contrato = await request.app.state.registro.autenticar(client_id, client_secret)
    if contrato is None:
        log.info("token_rechazado", extra={"campos": {"evento": "token_rechazado", "client_id": client_id[:64]}})
        cabeceras = {"WWW-Authenticate": 'Basic realm="solventa-partners"'} if basic is not None else None
        return _error(401, "invalid_client", "Credenciales del cliente inválidas.", cabeceras)

    emitido = request.app.state.tokens.emitir(client_id, sorted(contrato.endpoints), datetime.now(UTC))
    log.info("token_emitido", extra={"campos": {"evento": "token_emitido", "client_id": client_id}})
    return JSONResponse(
        content={
            "access_token": emitido.access_token,
            "token_type": "Bearer",
            "expires_in": emitido.expires_in,
            "scope": emitido.scope,
        },
        headers=_SIN_CACHE,
    )
