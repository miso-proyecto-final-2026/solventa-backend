"""Errores de dominio de Perfilamiento y su traducción al formato estándar de error.

Formato: {"error": {"code", "message", "details": [{"campo", "motivo"}], "trace_id"}}.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.proveedores.dominio import EstadoConsentimiento


class ClienteInvalido(Exception):
    """El identificador del cliente no tiene un formato aceptable."""


class PerfilNoEncontrado(Exception):
    """El cliente no tiene un perfil enriquecido todavía."""


class ConsentimientoNoVigente(Exception):
    """Revocado o inexistente: no se consulta nada y el intento queda registrado (EC-S1.2)."""

    def __init__(self, estado: EstadoConsentimiento) -> None:
        super().__init__(estado.value)
        self.estado = estado


class NoAutenticado(Exception):
    def __init__(self, mensaje: str = "Se requiere la clave de servicio.") -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


class SinPermiso(Exception):
    def __init__(self, mensaje: str = "La clave no tiene permiso.") -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


def cuerpo_error(code: str, message: str, detalles: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": detalles or [],
            "trace_id": str(uuid.uuid4()),
        }
    }


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ClienteInvalido)
    async def _cliente_invalido(_req: Request, _exc: ClienteInvalido) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=cuerpo_error(
                "VALIDATION_ERROR",
                "La solicitud no es válida.",
                [{"campo": "cliente_id", "motivo": "debe tener entre 1 y 64 letras, dígitos o . _ : -"}],
            ),
        )

    @app.exception_handler(PerfilNoEncontrado)
    async def _no_encontrado(_req: Request, _exc: PerfilNoEncontrado) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=cuerpo_error(
                "PERFIL_NO_ENCONTRADO", "El cliente no tiene un perfil enriquecido."
            ),
        )

    @app.exception_handler(ConsentimientoNoVigente)
    async def _sin_consentimiento(_req: Request, exc: ConsentimientoNoVigente) -> JSONResponse:
        # No se distingue «revocado» de «inexistente» en el mensaje: ambos bloquean igual.
        return JSONResponse(
            status_code=403,
            content=cuerpo_error(
                "CONSENTIMIENTO_NO_VIGENTE",
                "El cliente no tiene un consentimiento vigente para esta operación.",
            ),
        )

    @app.exception_handler(NoAutenticado)
    async def _no_autenticado(_req: Request, exc: NoAutenticado) -> JSONResponse:
        return JSONResponse(status_code=401, content=cuerpo_error("NO_AUTENTICADO", exc.mensaje))

    @app.exception_handler(SinPermiso)
    async def _sin_permiso(_req: Request, exc: SinPermiso) -> JSONResponse:
        return JSONResponse(status_code=403, content=cuerpo_error("SIN_PERMISO", exc.mensaje))
