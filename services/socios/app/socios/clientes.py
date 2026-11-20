"""Cliente HTTP de Cotización. Socios la conoce solo por su contrato REST (ADR-0001, ADR-0002: síncrono)."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.contexto import correlacion_id
from app.socios.errores import ServicioNoDisponible, SolicitudInvalida


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
