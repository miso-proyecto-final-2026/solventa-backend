"""Clientes HTTP de Cotización, Identidad y Suscripción. Socios los conoce solo por su contrato REST (ADR-0001, ADR-0002: síncrono)."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.contexto import correlacion_id
from app.socios.circuito import Circuito
from app.socios.traduccion import COTIZACION, IDENTIDAD, SUSCRIPCION, traducir


class Cotizaciones(Protocol):
    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]: ...

    async def disponible(self) -> bool: ...


class ClienteCotizacion:
    """`POST /v1/cotizaciones` de Cotización (HU12). Propaga el `X-Correlation-Id` del socio para la trazabilidad."""

    def __init__(self, http: httpx.AsyncClient, circuito: Circuito | None = None) -> None:
        self._http = http
        self._circuito = circuito or Circuito(COTIZACION)

    @staticmethod
    def _cabeceras() -> dict[str, str]:
        valor = correlacion_id.get()
        return {"X-Correlation-Id": valor} if valor else {}

    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]:
        r = await self._circuito.ejecutar(
            lambda: self._http.post(
                "/v1/cotizaciones",
                json={"cliente_id": cliente_id, "producto": producto, "datos": datos},
                headers=self._cabeceras(),
            )
        )
        if r.status_code in (200, 201):
            return r.json()
        raise traducir(COTIZACION, r)

    async def disponible(self) -> bool:
        try:
            r = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return r.status_code == 200


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

    def __init__(self, http: httpx.AsyncClient, clave_servicio: str, circuito: Circuito | None = None) -> None:
        self._http = http
        self._clave = clave_servicio
        self._circuito = circuito or Circuito(IDENTIDAD)

    async def identificar(self, tipo_documento: str, numero_documento: str) -> dict[str, Any] | None:
        r = await self._circuito.ejecutar(
            lambda: self._http.post(
                "/v1/interno/clientes/identificacion",
                json={"tipo_documento": tipo_documento, "numero_documento": numero_documento},
                headers=_cabeceras_servicio(self._clave),
            )
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None  # `identificar_exigiendo` lo convierte en CLIENTE_NO_REGISTRADO
        raise traducir(IDENTIDAD, r)


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


class ClienteSuscripcion:
    """Rutas internas `/v1/interno/socios` de Suscripción. `socio_client_id` es la atribución y el filtro de aislamiento."""

    def __init__(self, http: httpx.AsyncClient, clave_servicio: str, circuito: Circuito | None = None) -> None:
        self._http = http
        self._clave = clave_servicio
        self._circuito = circuito or Circuito(SUSCRIPCION)

    async def _enviar(self, metodo: str, ruta: str, **opciones: Any) -> httpx.Response:
        cabeceras = {**_cabeceras_servicio(self._clave), **opciones.pop("headers", {})}
        r = await self._circuito.ejecutar(
            lambda: self._http.request(metodo, f"/v1/interno/socios{ruta}", headers=cabeceras, **opciones)
        )
        if r.is_success:
            return r
        raise traducir(SUSCRIPCION, r)

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
