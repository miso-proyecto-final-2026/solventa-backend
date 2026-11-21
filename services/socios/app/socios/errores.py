"""Catálogo de errores de MS Socios y su traducción al formato estándar (HU34, ADR-0027).

Formato en `/partners/v1`: {"error": {"code", "message", "trace_id", "reason"?, "details"}}.
- `code` sale del catálogo (`CATALOGO`); es estable y es contra lo que programa el socio.
- `reason` (opcional) es el motivo de negocio, de una lista cerrada (`MOTIVOS`).
- `details` lleva solo los campos inválidos: [{"campo", "motivo"}].
Los mensajes son propios y en español: nunca se reenvía el mensaje de un servicio interno.
`/oauth/token` responde con el formato de RFC 6749 (ver `oauth.py`): es lo que esperan las librerías OAuth2.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.contexto import traza_id

log = logging.getLogger(__name__)

# code → (HTTP, mensaje por defecto, política de reintento para la documentación).
CATALOGO: dict[str, tuple[int, str, str]] = {
    "INVALID_REQUEST": (400, "La solicitud no es válida.", "No reintentar; corregir la solicitud."),
    "INVALID_TOKEN": (401, "Se requiere un token de acceso válido.", "No reintentar; obtener un token."),
    "TOKEN_EXPIRED": (
        401,
        "El token de acceso expiró; solicite uno nuevo en /oauth/token.",
        "Pedir un token nuevo y repetir.",
    ),
    "ENDPOINT_NOT_AUTHORIZED": (403, "El contrato del socio no habilita este endpoint.", "No reintentar."),
    "RESOURCE_NOT_FOUND": (404, "El recurso no existe.", "No reintentar."),
    "IDEMPOTENCY_CONFLICT": (
        409,
        "La clave de idempotencia ya se usó con otra solicitud. No reintente con la misma clave.",
        "No reintentar con la misma clave (salvo reason SOLICITUD_EN_PROCESO).",
    ),
    "RATE_LIMIT_EXCEEDED": (
        429,
        "Se excedió la cuota de solicitudes por segundo del contrato.",
        "Reintentar tras Retry-After.",
    ),
    "INTERNAL_ERROR": (500, "Error interno; reintente más tarde.", "Reintentar con retroceso 1-2-4-8-16 s."),
    "SERVICE_UNAVAILABLE": (
        503,
        "Un servicio necesario no responde; reintente más tarde.",
        "Reintentar con retroceso 1-2-4-8-16 s.",
    ),
    "SERVICE_DEGRADED": (
        503,
        "Un servicio necesario está degradado; reintente más tarde.",
        "Reintentar tras Retry-After.",
    ),
}

# reason → mensaje en español. Lista cerrada: una prueba verifica que toda `reason` usada exista aquí.
MOTIVOS: dict[str, str] = {
    "CLIENTE_NO_REGISTRADO": "El cliente no está registrado en Solventa.",
    "KYC_NO_VERIFICADO": "El cliente no ha verificado su identidad (KYC) en Solventa.",
    "CONSENTIMIENTO_NO_VIGENTE": "El cliente no tiene un consentimiento Open Finance vigente.",
    "COTIZACION_YA_USADA": "La cotización ya se usó en otra venta.",
    "COTIZACION_VENCIDA": "La cotización venció; cotice de nuevo.",
    "COTIZACION_SIN_CUENTA": "La cotización no está asociada a una cuenta que se pueda suscribir.",
    "SIN_METODO_PAGO": "No hay un método de pago válido para cobrar la póliza.",
    "POLIZA_NO_LISTA": "La póliza todavía no está lista para esta operación.",
    "FIRMA_NO_DISPONIBLE": "La firma no está disponible para esta venta.",
    "FIRMA_VENCIDA": "El plazo para firmar venció.",
    "DOCUMENTO_ALTERADO": "El documento que se firma no coincide con el emitido.",
    "OTP_INVALIDO": "El código de firma no es válido.",
    "FIRMA_FALLIDA": "No se pudo verificar la identidad del cliente al firmar; la venta se canceló.",
    "CERTIFICADO_NO_DISPONIBLE": "El certificado de la póliza no está disponible.",
    "SOLICITUD_EN_PROCESO": "La solicitud original con esta clave sigue en proceso; reintente con la misma clave.",
}


class ErrorSocios(Exception):
    code = "INVALID_REQUEST"

    def __init__(
        self,
        mensaje: str | None = None,
        detalles: list[dict[str, str]] | None = None,
        cabeceras: dict[str, str] | None = None,
        reason: str | None = None,
    ) -> None:
        if reason is not None and reason not in MOTIVOS:
            raise ValueError(f"reason fuera del catálogo: {reason}")
        self.status = CATALOGO[self.code][0]
        self.mensaje = mensaje or (MOTIVOS[reason] if reason else CATALOGO[self.code][1])
        super().__init__(self.mensaje)
        self.detalles = detalles or []
        self.cabeceras = cabeceras or {}
        self.reason = reason


class SolicitudInvalida(ErrorSocios):
    code = "INVALID_REQUEST"


class FaltaClaveIdempotencia(ErrorSocios):
    code = "INVALID_REQUEST"

    def __init__(self, mensaje: str | None = None, detalles: list[dict[str, str]] | None = None) -> None:
        super().__init__(mensaje or "Las escrituras exigen la cabecera X-Idempotency-Key.", detalles)


class ReglaDeNegocio(SolicitudInvalida):
    """Una regla de negocio no se cumple (cliente no apto, cotización usada, firma vencida...). El motivo va en `reason`."""

    def __init__(self, reason: str, detalles: list[dict[str, str]] | None = None) -> None:
        super().__init__(detalles=detalles, reason=reason)


class TokenInvalido(ErrorSocios):
    code = "INVALID_TOKEN"


class TokenExpirado(ErrorSocios):
    code = "TOKEN_EXPIRED"


class EndpointNoAutorizado(ErrorSocios):
    code = "ENDPOINT_NOT_AUTHORIZED"


class ConflictoIdempotencia(ErrorSocios):
    """La clave se reutilizó con otro cuerpo."""

    code = "IDEMPOTENCY_CONFLICT"


class SolicitudEnProceso(ConflictoIdempotencia):
    """La solicitud original con esta clave sigue en curso: el socio sí reintenta, con la misma clave, tras `Retry-After`."""

    def __init__(self, espera_s: int = 2) -> None:
        super().__init__(reason="SOLICITUD_EN_PROCESO", cabeceras={"Retry-After": str(espera_s)})


class CuotaExcedida(ErrorSocios):
    code = "RATE_LIMIT_EXCEEDED"


class RecursoNoEncontrado(ErrorSocios):
    code = "RESOURCE_NOT_FOUND"


class ErrorInterno(ErrorSocios):
    """Un error interno que no se puede traducir a algo que el socio corrija (p. ej. una clave de servicio rechazada)."""

    code = "INTERNAL_ERROR"


class ServicioNoDisponible(ErrorSocios):
    code = "SERVICE_UNAVAILABLE"


class ServicioDegradado(ErrorSocios):
    """El circuito hacia una dependencia está abierto: no se llama hasta el enfriamiento."""

    code = "SERVICE_DEGRADED"

    def __init__(self, mensaje: str, espera_s: int) -> None:
        super().__init__(mensaje, cabeceras={"Retry-After": str(max(1, espera_s))})


def cuerpo_error(
    code: str,
    message: str,
    detalles: list[dict[str, str]] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "trace_id": traza_id.get() or str(uuid.uuid4())}
    if reason:
        error["reason"] = reason
    error["details"] = detalles or []
    return {"error": error}


def _respuesta(status: int, code: str, mensaje: str | None = None, **opciones: Any) -> JSONResponse:
    # `X-Correlation-Id` lo agrega MiddlewareCorrelacion a toda respuesta, incluida esta.
    cabeceras = dict(opciones.pop("cabeceras", None) or {})
    return JSONResponse(
        status_code=status,
        content=cuerpo_error(code, mensaje or CATALOGO[code][1], **opciones),
        headers=cabeceras,
    )


def respuesta_error_interno() -> JSONResponse:
    """500 del catálogo. Lo usa el middleware de correlación, con el contexto de la solicitud todavía vivo."""
    return _respuesta(500, "INTERNAL_ERROR")


def _code_para_http(status: int) -> str:
    for code, (http, _, _) in CATALOGO.items():
        if http == status and code not in ("SERVICE_DEGRADED", "TOKEN_EXPIRED"):
            return code
    return "INVALID_REQUEST" if status < 500 else "INTERNAL_ERROR"


def registrar_errores(app: FastAPI) -> None:
    @app.exception_handler(ErrorSocios)
    async def _error(_req: Request, exc: ErrorSocios) -> JSONResponse:
        return _respuesta(exc.status, exc.code, exc.mensaje, detalles=exc.detalles, reason=exc.reason, cabeceras=exc.cabeceras)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Rutas inexistentes y métodos no permitidos: el mensaje es el del catálogo, nunca el `detail` de Starlette.
        code = _code_para_http(exc.status_code)
        cabeceras = {k: v for k, v in (exc.headers or {}).items() if k.lower() == "allow"}
        return _respuesta(exc.status_code, code, cabeceras=cabeceras)

    @app.exception_handler(RequestValidationError)
    async def _validacion(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # Solo el campo y el mensaje de Pydantic: `input` y `ctx` podrían repetir un dato personal o un secreto.
        detalles = [
            {"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "motivo": e["msg"]} for e in exc.errors()
        ]
        return _respuesta(400, "INVALID_REQUEST", detalles=detalles)

    @app.exception_handler(Exception)
    async def _inesperado(_req: Request, exc: Exception) -> JSONResponse:
        # Respaldo: el 500 normal se resuelve dentro de MiddlewareCorrelacion, donde el trace_id sigue vivo.
        log.error("error_inesperado", exc_info=exc)
        return respuesta_error_interno()


def publicar_openapi_sin_422(app: FastAPI) -> None:
    """FastAPI documenta un 422 propio para toda ruta con parámetros; aquí la validación responde 400 con `ErrorOut`."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            esquema = original()
            for metodos in esquema["paths"].values():
                for operacion in metodos.values():
                    operacion["responses"].pop("422", None)
            for nombre in ("HTTPValidationError", "ValidationError"):
                esquema.get("components", {}).get("schemas", {}).pop(nombre, None)
        return app.openapi_schema or esquema

    app.openapi = openapi  # type: ignore[method-assign]
