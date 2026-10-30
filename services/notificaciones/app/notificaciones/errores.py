"""Errores de dominio de Notificaciones y su traducción al formato estándar de error.

Formato: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.contexto import traza_id


class ErrorNotificaciones(Exception):
    status = 400
    code = "ERROR"
    mensaje = ""

    def __init__(self, mensaje: str | None = None, detalles: list[dict[str, str]] | None = None) -> None:
        super().__init__(mensaje or self.mensaje)
        self.mensaje = mensaje or self.mensaje
        self.detalles = detalles or []


class NoAutenticado(ErrorNotificaciones):
    status, code, mensaje = 401, "NO_AUTENTICADO", "Se requiere una sesión válida."


class SinPermiso(ErrorNotificaciones):
    status, code, mensaje = 403, "SIN_PERMISO", "No tiene permiso para esta operación."


class NotificacionNoEncontrada(ErrorNotificaciones):
    status, code, mensaje = 404, "NOTIFICACION_NO_ENCONTRADA", "La notificación no existe."


class DependenciaNoDisponible(ErrorNotificaciones):
    status, code, mensaje = 503, "DEPENDENCIA_NO_DISPONIBLE", "Un servicio necesario no responde; reintente."


def cuerpo_error(code: str, message: str, detalles: list[dict[str, str]] | None = None, trace_id: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": detalles or [],
            "trace_id": trace_id or traza_id.get() or str(uuid.uuid4()),
        }
    }


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ErrorNotificaciones)
    async def _error(_req: Request, exc: ErrorNotificaciones) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=cuerpo_error(exc.code, exc.mensaje, exc.detalles))

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validacion(_req: Request, exc: RequestValidationError) -> JSONResponse:
        detalles = [
            {"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "motivo": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content=cuerpo_error("VALIDATION_ERROR", "La solicitud no es válida.", detalles)
        )
