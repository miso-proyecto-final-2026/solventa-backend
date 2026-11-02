"""Errores de dominio de Siniestros y su traducción al formato estándar de error.

Formato: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class EventoInvalido(Exception):
    def __init__(self, detalles: list[dict[str, str]], codigo: str = "EVENTO_INVALIDO") -> None:
        super().__init__("evento inválido")
        self.detalles = detalles
        self.codigo = codigo


class BusNoDisponible(Exception):
    """El evento quedó registrado pero el bus no lo aceptó: el proveedor debe reenviarlo."""


class NoAutenticado(Exception):
    """Sin llave de API, o con una llave que no corresponde a ningún proveedor."""

    def __init__(self, mensaje: str = "Se requiere una llave de API válida.") -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


def cuerpo_error(
    code: str, message: str, detalles: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": detalles or [],
            "trace_id": str(uuid.uuid4()),
        }
    }


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(EventoInvalido)
    async def _evento_invalido(_req: Request, exc: EventoInvalido) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=cuerpo_error(exc.codigo, "El evento no es válido.", exc.detalles),
        )

    @app.exception_handler(NoAutenticado)
    async def _no_autenticado(_req: Request, exc: NoAutenticado) -> JSONResponse:
        return JSONResponse(status_code=401, content=cuerpo_error("NO_AUTENTICADO", exc.mensaje))

    @app.exception_handler(BusNoDisponible)
    async def _bus_no_disponible(_req: Request, _exc: BusNoDisponible) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content=cuerpo_error(
                "BUS_SATURADO",
                "El servicio está saturado; el evento quedó registrado, reenvíelo con el mismo evento_id.",
            ),
        )
