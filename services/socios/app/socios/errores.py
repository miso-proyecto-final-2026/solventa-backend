"""Errores de MS Socios y su traducción al formato estándar de error.

Formato en `/partners/v1`: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
Los códigos son los de la guía de integración de socios (§4); el catálogo completo es de HU34.
`/oauth/token` responde con el formato de RFC 6749 (ver `oauth.py`): es lo que esperan las librerías OAuth2.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.contexto import traza_id

log = logging.getLogger(__name__)


class ErrorSocios(Exception):
    status = 400
    code = "INVALID_REQUEST"
    mensaje = ""

    def __init__(
        self,
        mensaje: str | None = None,
        detalles: list[dict[str, str]] | None = None,
        cabeceras: dict[str, str] | None = None,
    ) -> None:
        super().__init__(mensaje or self.mensaje)
        self.mensaje = mensaje or self.mensaje
        self.detalles = detalles or []
        self.cabeceras = cabeceras or {}


class SolicitudInvalida(ErrorSocios):
    status, code, mensaje = 422, "INVALID_REQUEST", "La solicitud no es válida."


class FaltaClaveIdempotencia(ErrorSocios):
    status, code, mensaje = 400, "INVALID_REQUEST", "Las escrituras exigen la cabecera X-Idempotency-Key."


class TokenInvalido(ErrorSocios):
    status, code, mensaje = 401, "INVALID_TOKEN", "Se requiere un token de acceso válido."


class TokenExpirado(ErrorSocios):
    status, code, mensaje = 401, "TOKEN_EXPIRED", "El token de acceso expiró; solicite uno nuevo en /oauth/token."


class EndpointNoAutorizado(ErrorSocios):
    status, code, mensaje = 403, "ENDPOINT_NOT_AUTHORIZED", "El contrato del socio no habilita este endpoint."


class ConflictoIdempotencia(ErrorSocios):
    status, code, mensaje = (
        409,
        "IDEMPOTENCY_CONFLICT",
        "La clave de idempotencia ya se usó con otra solicitud o la original sigue en proceso. No reintente con la misma clave.",
    )


class CuotaExcedida(ErrorSocios):
    status, code, mensaje = 429, "RATE_LIMIT_EXCEEDED", "Se excedió la cuota de solicitudes por segundo del contrato."


class RecursoNoEncontrado(ErrorSocios):
    status, code, mensaje = 404, "RESOURCE_NOT_FOUND", "El recurso no existe."


class ConflictoDeNegocio(ErrorSocios):
    """La operación choca con el estado del recurso (cotización ya usada, firma vencida...). El motivo va en `details`.

    Usa el código del catálogo para 409 mientras HU34 define los de negocio (ADR-0026).
    """

    status, code, mensaje = 409, "IDEMPOTENCY_CONFLICT", "La operación no se puede completar en el estado actual del recurso."


class ServicioNoDisponible(ErrorSocios):
    status, code, mensaje = 503, "SERVICE_UNAVAILABLE", "Un servicio necesario no responde; reintente más tarde."


def cuerpo_error(code: str, message: str, detalles: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": detalles or [],
            "trace_id": traza_id.get() or str(uuid.uuid4()),
        }
    }


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ErrorSocios)
    async def _error(_req: Request, exc: ErrorSocios) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=cuerpo_error(exc.code, exc.mensaje, exc.detalles), headers=exc.cabeceras
        )

    @app.exception_handler(RequestValidationError)
    async def _validacion(_req: Request, exc: RequestValidationError) -> JSONResponse:
        detalles = [
            {"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "motivo": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content=cuerpo_error("INVALID_REQUEST", "La solicitud no es válida.", detalles))

    @app.exception_handler(Exception)
    async def _inesperado(_req: Request, exc: Exception) -> JSONResponse:
        log.exception("error_inesperado")
        return JSONResponse(status_code=500, content=cuerpo_error("INTERNAL_ERROR", "Error interno; reintente más tarde."))
