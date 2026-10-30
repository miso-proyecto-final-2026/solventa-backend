"""Errores de dominio de Suscripción y su traducción al formato estándar de error.

Formato: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.contexto import traza_id


class ErrorSuscripcion(Exception):
    status = 400
    code = "ERROR"
    mensaje = ""

    def __init__(self, mensaje: str | None = None, detalles: list[dict[str, str]] | None = None) -> None:
        super().__init__(mensaje or self.mensaje)
        self.mensaje = mensaje or self.mensaje
        self.detalles = detalles or []


class NoAutenticado(ErrorSuscripcion):
    status, code, mensaje = 401, "NO_AUTENTICADO", "Se requiere una sesión válida."


class SolicitudInvalida(ErrorSuscripcion):
    status, code, mensaje = 422, "VALIDATION_ERROR", "La solicitud no es válida."


class CotizacionNoEncontrada(ErrorSuscripcion):
    status, code, mensaje = 404, "COTIZACION_NO_ENCONTRADA", "La cotización no existe."


class SuscripcionNoEncontrada(ErrorSuscripcion):
    status, code, mensaje = 404, "SUSCRIPCION_NO_ENCONTRADA", "La suscripción no existe."


class PolizaNoEncontrada(ErrorSuscripcion):
    status, code, mensaje = 404, "POLIZA_NO_ENCONTRADA", "La póliza no existe."


class AccesoDenegado(ErrorSuscripcion):
    status, code, mensaje = 403, "ACCESO_DENEGADO", "No tiene acceso a este recurso."


class PolizaNoLista(ErrorSuscripcion):
    status, code, mensaje = 409, "POLIZA_NO_LISTA", "El documento de la póliza aún no está generado."


class FirmaNoDisponible(ErrorSuscripcion):
    status, code, mensaje = 409, "FIRMA_NO_DISPONIBLE", "La suscripción no está en el paso de firma."


class FirmaVencida(ErrorSuscripcion):
    status, code, mensaje = (
        409,
        "FIRMA_VENCIDA",
        "El plazo para firmar terminó. Le devolveremos el cobro; puede volver a cotizar cuando quiera.",
    )


class FirmaFallida(ErrorSuscripcion):
    status, code, mensaje = (
        409,
        "FIRMA_FALLIDA",
        "No pudimos verificar su identidad. Le devolveremos el cobro; puede volver a cotizar cuando quiera.",
    )


class AutenticacionInvalida(ErrorSuscripcion):
    status, code, mensaje = 403, "OTP_INVALIDO", "El código no es correcto."


class DocumentoAlterado(ErrorSuscripcion):
    status, code, mensaje = (
        409,
        "DOCUMENTO_ALTERADO",
        "El documento cambió desde que lo revisó. Ábralo de nuevo antes de firmar.",
    )


class FirmaNoEncontrada(ErrorSuscripcion):
    status, code, mensaje = 404, "FIRMA_NO_ENCONTRADA", "La póliza aún no está firmada."


class CotizacionVencida(ErrorSuscripcion):
    status, code, mensaje = 409, "COTIZACION_VENCIDA", "La cotización venció; genere una nueva."


class CotizacionYaUsada(ErrorSuscripcion):
    status, code, mensaje = 409, "COTIZACION_YA_USADA", "La cotización ya tiene una suscripción en curso."


class ClaveReutilizada(ErrorSuscripcion):
    status, code, mensaje = (
        409,
        "CLAVE_IDEMPOTENCIA_REUTILIZADA",
        "La clave de idempotencia ya se usó con otra cotización.",
    )


class DependenciaNoDisponible(ErrorSuscripcion):
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
    @app.exception_handler(ErrorSuscripcion)
    async def _error(_req: Request, exc: ErrorSuscripcion) -> JSONResponse:
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
