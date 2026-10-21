"""Errores de dominio de Cotización y su traducción al formato estándar de error.

El dominio lanza estos errores; solo `registrar_errores` (y la ruta de
validación en `api.py`) los convierten a HTTP. Formato de la respuesta:
{"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class DetalleError:
    campo: str
    motivo: str


class ErrorValidacion(Exception):
    """Producto inexistente o datos incompletos / fuera de rango. No se calcula."""

    def __init__(self, detalles: list[DetalleError]) -> None:
        super().__init__("; ".join(f"{d.campo}: {d.motivo}" for d in detalles))
        self.detalles = detalles


class CotizacionNoEncontrada(Exception):
    pass


class DesgloseNoDisponible(Exception):
    """Cotización anterior a HU14: existe pero no tiene desglose persistido."""


class ErrorDesglose(Exception):
    """El desglose no explica el precio (suma o recálculo inconsistentes). No se emite."""


class NoAutenticado(Exception):
    """Falta la credencial o no es válida (auditor por clave de servicio, asesor por token)."""

    def __init__(self, mensaje: str = "Se requiere la credencial de auditoría.") -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


class SinPermiso(Exception):
    """La credencial es válida pero no tiene el permiso requerido."""

    def __init__(
        self, mensaje: str = "La credencial no tiene permiso de auditoría."
    ) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


class ErrorReglas(Exception):
    """Las reglas versionadas no existen o son inválidas (error de configuración)."""


def cuerpo_error(
    code: str, message: str, detalles: list[DetalleError] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": [{"campo": d.campo, "motivo": d.motivo} for d in detalles or []],
            "trace_id": str(uuid.uuid4()),
        }
    }


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ErrorValidacion)
    async def _validacion(_req: Request, exc: ErrorValidacion) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=cuerpo_error(
                "VALIDATION_ERROR", "La solicitud no es válida.", exc.detalles
            ),
        )

    @app.exception_handler(CotizacionNoEncontrada)
    async def _no_encontrada(
        _req: Request, _exc: CotizacionNoEncontrada
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=cuerpo_error(
                "COTIZACION_NO_ENCONTRADA", "La cotización no existe."
            ),
        )

    @app.exception_handler(DesgloseNoDisponible)
    async def _desglose_no_disponible(
        _req: Request, _exc: DesgloseNoDisponible
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=cuerpo_error(
                "DESGLOSE_NO_DISPONIBLE",
                "La cotización no tiene desglose de precio registrado.",
            ),
        )

    @app.exception_handler(ErrorDesglose)
    async def _error_desglose(_req: Request, _exc: ErrorDesglose) -> JSONResponse:
        # El detalle va al log del servicio; el cliente no recibe datos internos.
        return JSONResponse(
            status_code=500,
            content=cuerpo_error(
                "DESGLOSE_INCONSISTENTE",
                "No se pudo emitir la cotización. Intente de nuevo más tarde.",
            ),
        )

    @app.exception_handler(NoAutenticado)
    async def _no_autenticado(_req: Request, exc: NoAutenticado) -> JSONResponse:
        return JSONResponse(
            status_code=401, content=cuerpo_error("NO_AUTENTICADO", exc.mensaje)
        )

    @app.exception_handler(SinPermiso)
    async def _sin_permiso(_req: Request, exc: SinPermiso) -> JSONResponse:
        return JSONResponse(
            status_code=403, content=cuerpo_error("SIN_PERMISO", exc.mensaje)
        )
