"""Clientes HTTP de Cotización, Identidad y Suscripción. Socios los conoce solo por su contrato REST (ADR-0001, ADR-0002: síncrono)."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.contexto import correlacion_id
from app.socios.errores import (
    ConflictoDeNegocio,
    RecursoNoEncontrado,
    ServicioNoDisponible,
    SolicitudInvalida,
)


class Cotizaciones(Protocol):
    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]: ...

    async def disponible(self) -> bool: ...


class ClienteCotizacion:
    """`POST /v1/cotizaciones` de Cotización (HU12). Propaga el `X-Correlation-Id` del socio para la trazabilidad."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @staticmethod
    def _cabeceras() -> dict[str, str]:
        valor = correlacion_id.get()
        return {"X-Correlation-Id": valor} if valor else {}

    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._http.post(
                "/v1/cotizaciones",
                json={"cliente_id": cliente_id, "producto": producto, "datos": datos},
                headers=self._cabeceras(),
            )
        except httpx.HTTPError as error:
            raise ServicioNoDisponible("Cotización no responde; reintente más tarde.") from error
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code in (400, 422):
            raise SolicitudInvalida(detalles=_detalles(r))
        raise ServicioNoDisponible(f"Cotización respondió {r.status_code}; reintente más tarde.")

    async def disponible(self) -> bool:
        try:
            r = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return r.status_code == 200


def _detalles(r: httpx.Response) -> list[dict[str, str]]:
    """Los detalles de validación de Cotización (producto o datos inválidos), sin el resto de su error."""
    try:
        detalles = r.json().get("error", {}).get("details", [])
    except ValueError:
        return []
    return [{"campo": str(d.get("campo", "")), "motivo": str(d.get("motivo", ""))} for d in detalles if isinstance(d, dict)]


# --- Identidad (HU33) -------------------------------------------------------------------------------------------------


class Identidad(Protocol):
    async def identificar(self, tipo_documento: str, numero_documento: str) -> dict[str, Any] | None:
        """`{cliente_id, estado_kyc, consentimiento_vigente}`, o None si no hay un cliente con ese documento."""
        ...


def _cabeceras_servicio(clave: str) -> dict[str, str]:
    cabeceras = {"X-Service-Key": clave}
    valor = correlacion_id.get()
    if valor:
        cabeceras["X-Correlation-Id"] = valor
    return cabeceras


class ClienteIdentidad:
    """`POST /v1/interno/clientes/identificacion` de Identidad. El documento va en el cuerpo, nunca en la URL ni en los logs."""

    def __init__(self, http: httpx.AsyncClient, clave_servicio: str) -> None:
        self._http = http
        self._clave = clave_servicio

    async def identificar(self, tipo_documento: str, numero_documento: str) -> dict[str, Any] | None:
        try:
            r = await self._http.post(
                "/v1/interno/clientes/identificacion",
                json={"tipo_documento": tipo_documento, "numero_documento": numero_documento},
                headers=_cabeceras_servicio(self._clave),
            )
        except httpx.HTTPError as error:
            raise ServicioNoDisponible("Identidad no responde; reintente más tarde.") from error
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 422:
            # Identidad dice que el documento no es válido; aquí no se repite.
            raise SolicitudInvalida(
                detalles=[{"campo": "cliente", "motivo": "El tipo o el número de documento no son válidos."}]
            )
        raise ServicioNoDisponible(f"Identidad respondió {r.status_code}; reintente más tarde.")


# --- Suscripción (HU33) -----------------------------------------------------------------------------------------------


class Suscripciones(Protocol):
    async def iniciar(
        self,
        socio_client_id: str,
        cliente_id: str,
        clave: str,
        *,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str,
    ) -> dict[str, Any]: ...

    async def detalle(self, suscripcion_id: str, socio_client_id: str) -> dict[str, Any]: ...

    async def documento_firma(self, suscripcion_id: str, socio_client_id: str) -> dict[str, Any]: ...

    async def documento_pdf(self, suscripcion_id: str, socio_client_id: str) -> bytes: ...

    async def solicitar_codigo(self, suscripcion_id: str, socio_client_id: str) -> None: ...

    async def firmar(self, suscripcion_id: str, socio_client_id: str, firma: dict[str, Any]) -> dict[str, Any]: ...


def _error_de(r: httpx.Response) -> tuple[str, str, list[dict[str, str]]]:
    """Código, mensaje y detalles del error de Suscripción; vacíos si la respuesta no trae el formato esperado."""
    try:
        e = r.json().get("error", {})
    except ValueError:
        return "", "", []
    detalles = [
        {"campo": str(d.get("campo", "")), "motivo": str(d.get("motivo", ""))}
        for d in e.get("details", [])
        if isinstance(d, dict)
    ]
    return str(e.get("code", "")), str(e.get("message", "")), detalles


def traducir_error(r: httpx.Response) -> Exception:
    """Errores de Suscripción al formato de Socios (ADR-0026). El catálogo definitivo es de HU34.

    404 → RESOURCE_NOT_FOUND; 409 → 409 con el motivo de negocio en `details`; 400/403/422 → INVALID_REQUEST (422);
    una clave de servicio rechazada (401), una caída o cualquier otro fallo → 503 sin revelar el detalle interno.
    """
    codigo, mensaje, detalles = _error_de(r)
    motivo = [{"campo": "motivo", "motivo": codigo}] if codigo else []
    if r.status_code == 404:
        return RecursoNoEncontrado()
    if r.status_code == 409:
        return ConflictoDeNegocio(mensaje or None, detalles or motivo)
    if r.status_code in (400, 403, 422):
        return SolicitudInvalida(detalles=detalles or motivo)
    return ServicioNoDisponible(f"Suscripción respondió {r.status_code}; reintente más tarde.")


class ClienteSuscripcion:
    """Rutas internas `/v1/interno/socios` de Suscripción. `socio_client_id` es la atribución y el filtro de aislamiento."""

    def __init__(self, http: httpx.AsyncClient, clave_servicio: str) -> None:
        self._http = http
        self._clave = clave_servicio

    async def _enviar(self, metodo: str, ruta: str, **opciones: Any) -> httpx.Response:
        cabeceras = {**_cabeceras_servicio(self._clave), **opciones.pop("headers", {})}
        try:
            r = await self._http.request(metodo, f"/v1/interno/socios{ruta}", headers=cabeceras, **opciones)
        except httpx.HTTPError as error:
            raise ServicioNoDisponible("Suscripción no responde; reintente más tarde.") from error
        if r.is_success:
            return r
        raise traducir_error(r)

    async def iniciar(
        self,
        socio_client_id: str,
        cliente_id: str,
        clave: str,
        *,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str,
    ) -> dict[str, Any]:
        r = await self._enviar(
            "POST",
            "/suscripciones",
            json={
                "socio_client_id": socio_client_id,
                "cliente_id": cliente_id,
                "cotizacion_id": cotizacion_id,
                "metodo_pago": metodo_pago,
                "acepta_terminos": acepta_terminos,
                "periodicidad_pago": periodicidad_pago,
            },
            headers={"Idempotency-Key": clave},
        )
        return r.json()

    async def detalle(self, suscripcion_id: str, socio_client_id: str) -> dict[str, Any]:
        r = await self._enviar("GET", f"/suscripciones/{suscripcion_id}", params={"socio_client_id": socio_client_id})
        return r.json()

    async def documento_firma(self, suscripcion_id: str, socio_client_id: str) -> dict[str, Any]:
        r = await self._enviar(
            "GET", f"/suscripciones/{suscripcion_id}/documento-firma", params={"socio_client_id": socio_client_id}
        )
        return r.json()

    async def documento_pdf(self, suscripcion_id: str, socio_client_id: str) -> bytes:
        r = await self._enviar(
            "GET", f"/suscripciones/{suscripcion_id}/documento-firma/pdf", params={"socio_client_id": socio_client_id}
        )
        return r.content

    async def solicitar_codigo(self, suscripcion_id: str, socio_client_id: str) -> None:
        await self._enviar(
            "POST", f"/suscripciones/{suscripcion_id}/firma/codigo", json={"socio_client_id": socio_client_id}
        )

    async def firmar(self, suscripcion_id: str, socio_client_id: str, firma: dict[str, Any]) -> dict[str, Any]:
        r = await self._enviar(
            "POST", f"/suscripciones/{suscripcion_id}/firma", json={"socio_client_id": socio_client_id, **firma}
        )
        return r.json()
