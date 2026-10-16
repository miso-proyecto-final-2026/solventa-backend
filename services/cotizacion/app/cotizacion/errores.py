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
