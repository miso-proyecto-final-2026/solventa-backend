"""Traducción de los errores de Cotización, Identidad y Suscripción al catálogo de socios (HU34, ADR-0027).

Un solo lugar decide qué ve el socio cuando un servicio interno responde con error:
- el `message`, los `details` internos y el `trace_id` del servicio nunca llegan al socio (EC-I1.2);
- el socio recibe el `trace_id` de su solicitud en Socios, y el log `error_traducido` lo enlaza con el del servicio
  (`trace_id_origen`), de modo que con el que reporta el socio se llega al log interno.
"""

from __future__ import annotations

import logging
import re

import httpx

from app.socios.errores import (
    ConflictoIdempotencia,
    ErrorInterno,
    ErrorSocios,
    RecursoNoEncontrado,
    ReglaDeNegocio,
    ServicioNoDisponible,
    SolicitudInvalida,
)

log = logging.getLogger(__name__)

COTIZACION = "cotizacion"
IDENTIDAD = "identidad"
SUSCRIPCION = "suscripcion"

NOMBRES = {COTIZACION: "Cotización", IDENTIDAD: "Identidad", SUSCRIPCION: "Suscripción"}

# Suscripción: lo que significa «no existe o no es suyo» (aislamiento, ADR-0026 §6).
NO_ENCONTRADO_SUSCRIPCION = frozenset(
    {"SUSCRIPCION_NO_ENCONTRADA", "COTIZACION_NO_ENCONTRADA", "POLIZA_NO_ENCONTRADA", "FIRMA_NO_ENCONTRADA", "ACCESO_DENEGADO"}
)
# Suscripción: reglas de negocio que el socio puede corregir; el `code` de origen es el `reason`.
REGLAS_SUSCRIPCION = frozenset(
    {
        "COTIZACION_YA_USADA",
        "COTIZACION_VENCIDA",
        "COTIZACION_SIN_CUENTA",
        "SIN_METODO_PAGO",
        "POLIZA_NO_LISTA",
        "FIRMA_NO_DISPONIBLE",
        "FIRMA_VENCIDA",
        "FIRMA_FALLIDA",
        "DOCUMENTO_ALTERADO",
        "OTP_INVALIDO",
        "CERTIFICADO_NO_DISPONIBLE",
    }
)
_CODIGO_VALIDO = re.compile(r"^[A-Z0-9_]{1,60}$")
_TRAZA_VALIDA = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _error_de(r: httpx.Response) -> tuple[str, str, list[dict[str, str]]]:
    """`code`, `trace_id` y `details` del error del servicio; vacíos si la respuesta no trae el formato esperado."""
    try:
        e = r.json().get("error", {})
    except (ValueError, AttributeError):
        return "", "", []
    if not isinstance(e, dict):
        return "", "", []
    codigo = str(e.get("code", ""))
    traza = str(e.get("trace_id", ""))
    detalles = [
        {"campo": str(d.get("campo", "")), "motivo": str(d.get("motivo", ""))}
        for d in e.get("details", [])
        if isinstance(d, dict)
    ]
    return (
        codigo if _CODIGO_VALIDO.match(codigo) else "",
        traza if _TRAZA_VALIDA.match(traza) else "",
        detalles,
    )


def _registrar(servicio: str, r: httpx.Response, codigo: str, traza_origen: str, traducido: ErrorSocios) -> None:
    log.warning(
        "error_traducido",
        extra={"campos": {
            "evento": "error_traducido",
            "servicio": servicio,
            "status_origen": r.status_code,
            "code_origen": codigo,
            "trace_id_origen": traza_origen,
            "code": traducido.code,
            "reason": traducido.reason,
        }},
    )


def es_fallo_de_dependencia(r: httpx.Response) -> bool:
    """Un 5xx cuenta como fallo para el circuito; un 4xx no (el servicio respondió, el problema es de la solicitud)."""
    return r.status_code >= 500


def traducir(servicio: str, r: httpx.Response) -> ErrorSocios:
    """El error del catálogo que corresponde a una respuesta de error (no 2xx) de un servicio interno."""
    codigo, traza_origen, detalles = _error_de(r)
    traducido = _traducir(servicio, r.status_code, codigo, detalles)
    _registrar(servicio, r, codigo, traza_origen, traducido)
    return traducido


def _traducir(servicio: str, status: int, codigo: str, detalles: list[dict[str, str]]) -> ErrorSocios:
    nombre = NOMBRES[servicio]
    if status >= 500:
        # También DEPENDENCIA_NO_DISPONIBLE de Suscripción: vive, pero le falla una dependencia suya.
        return ServicioNoDisponible(f"{nombre} no responde; reintente más tarde.")

    if servicio == COTIZACION:
        if status in (400, 422):
            return SolicitudInvalida(detalles=detalles)
        if codigo == "COTIZACION_NO_ENCONTRADA":
            return RecursoNoEncontrado()
    elif servicio == IDENTIDAD:
        if status == 404:
            return ReglaDeNegocio("CLIENTE_NO_REGISTRADO")
        if status == 422:
            return SolicitudInvalida(detalles=[{"campo": "cliente", "motivo": "El tipo o el número de documento no son válidos."}])
    elif servicio == SUSCRIPCION:
        if codigo in NO_ENCONTRADO_SUSCRIPCION or status == 404:
            return RecursoNoEncontrado()
        if codigo == "CLAVE_IDEMPOTENCIA_REUTILIZADA":
            return ConflictoIdempotencia()
        if codigo in REGLAS_SUSCRIPCION:
            return ReglaDeNegocio(codigo)
        if status == 422:
            return SolicitudInvalida(detalles=detalles)

    # 401 (clave de servicio rechazada) o un 4xx sin traducción: es una falla de configuración nuestra, no del socio.
    return ErrorInterno()
