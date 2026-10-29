"""Errores de dominio de Pagos y su traducción al formato estándar de error.

Formato: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.contexto import traza_id


class ErrorPagos(Exception):
    status = 400
    code = "ERROR"
    mensaje = ""

    def __init__(self, mensaje: str | None = None, detalles: list[dict[str, str]] | None = None) -> None:
        super().__init__(mensaje or self.mensaje)
        self.mensaje = mensaje or self.mensaje
        self.detalles = detalles or []


class NoAutenticado(ErrorPagos):
    status, code, mensaje = 401, "NO_AUTENTICADO", "Se requiere la cabecera X-Service-Key."


class SinPermiso(ErrorPagos):
    status, code, mensaje = 403, "SIN_PERMISO", "La clave de servicio no es válida."


class SolicitudInvalida(ErrorPagos):
    status, code, mensaje = 422, "VALIDATION_ERROR", "La solicitud no es válida."


class DatoDeTarjetaNoPermitido(ErrorPagos):
    """Se recibió algo con forma de número de tarjeta. Solventa solo acepta el token de la pasarela (SAQ-A)."""

    status, code, mensaje = 422, "DATO_DE_TARJETA_NO_PERMITIDO", "Envíe el token de la pasarela, nunca el número de tarjeta."


class TransaccionNoEncontrada(ErrorPagos):
    status, code, mensaje = 404, "TRANSACCION_NO_ENCONTRADA", "La transacción no existe."


class ClaveReutilizada(ErrorPagos):
    status, code, mensaje = (
        409,
        "CLAVE_IDEMPOTENCIA_REUTILIZADA",
        "La clave de idempotencia ya se usó con otro cobro.",
    )


class TokenNoReconocido(ErrorPagos):
    status, code, mensaje = 422, "TOKEN_NO_RECONOCIDO", "La pasarela no reconoce el token."


class PasarelaNoResponde(ErrorPagos):
    status, code, mensaje = 503, "PASARELA_NO_DISPONIBLE", "La pasarela no responde; reintente."


def cuerpo_error(code: str, message: str, detalles: list[dict[str, str]] | None = None, trace_id: str | None = None) -> dict[str, Any]:
    traza = trace_id or traza_id.get() or str(uuid.uuid4())
    return {"error": {"code": code, "message": message, "details": detalles or [], "trace_id": traza}}


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ErrorPagos)
    async def _error(_req: Request, exc: ErrorPagos) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=cuerpo_error(exc.code, exc.mensaje, exc.detalles))

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validacion(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # Solo el campo y el motivo: el valor recibido no se devuelve (podría ser un dato de tarjeta).
        detalles = [
            {"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "motivo": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content=cuerpo_error("VALIDATION_ERROR", "La solicitud no es válida.", detalles)
        )
