"""Linaje del perfil para el asesor (HU24): Cotización hace de fachada hacia MS Perfilamiento.

La consola llega con el JWT del asesor, que valida el router `/v1/asesor`. Aquí se consulta a
Perfilamiento por REST síncrono (ADR-0002) con la clave de servicio propia de Cotización y se le
informa a nombre de qué asesor consulta (`X-Consultado-Por`), para que su auditoría lo registre.
No se importa código de Perfilamiento ni se leen sus tablas (ADR-0001).

El linaje es el del último perfil enriquecido en Perfilamiento, que no siempre es el perfil con que
Cotización calculó el precio: Cotización tiene su propia réplica de proveedores (HU09).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.contexto import correlacion_id
from app.cotizacion.errores import LinajeNoDisponible, LinajeNoEncontrado
from app.logs import ganchos_http

log = logging.getLogger("solventa.linaje")


class LinajePort(Protocol):
    async def obtener(self, cliente_id: str, asesor_id: str) -> dict[str, Any]: ...

    async def cerrar(self) -> None: ...


class LinajeHttp:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 1.0,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = cliente or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, event_hooks=ganchos_http()
        )
        self._clave = api_key

    async def obtener(self, cliente_id: str, asesor_id: str) -> dict[str, Any]:
        cabeceras = {"X-Service-Key": self._clave, "X-Consultado-Por": asesor_id}
        if correlacion := correlacion_id.get():
            cabeceras["X-Correlation-Id"] = correlacion
        ruta = f"/v1/perfilamiento/{quote(cliente_id, safe='')}/linaje"
        try:
            respuesta = await self._http.get(ruta, headers=cabeceras)
        except httpx.HTTPError as exc:
            log.warning("perfilamiento_no_disponible error=%s", type(exc).__name__)
            raise LinajeNoDisponible from exc
        if respuesta.status_code == 404:
            raise LinajeNoEncontrado
        if respuesta.status_code in (401, 403):
            # Error de configuración (clave ausente o rotada), no del asesor: se ve en el log.
            log.error("perfilamiento_rechazo_la_clave status=%s", respuesta.status_code)
            raise LinajeNoDisponible
        if respuesta.status_code != 200:
            log.warning("perfilamiento_respuesta_inesperada status=%s", respuesta.status_code)
            raise LinajeNoDisponible
        try:
            cuerpo = respuesta.json()
        except ValueError as exc:
            log.warning("perfilamiento_cuerpo_invalido")
            raise LinajeNoDisponible from exc
        if not isinstance(cuerpo, dict):
            log.warning("perfilamiento_cuerpo_invalido")
            raise LinajeNoDisponible
        return cuerpo

    async def cerrar(self) -> None:
        await self._http.aclose()


class LinajeSinConfigurar:
    """Sin URL o sin clave de Perfilamiento: el linaje responde «no disponible» (503)."""

    async def obtener(self, cliente_id: str, asesor_id: str) -> dict[str, Any]:
        raise LinajeNoDisponible

    async def cerrar(self) -> None:
        return None


def construir_linaje(env: dict[str, str] | None = None) -> LinajePort:
    """El linaje es secundario y Cotización es crítica (HA01): sin configuración arranca igual.

    A diferencia de las demás dependencias, que fallan cerrado, si falta `PERFILAMIENTO_URL` o
    `PERFILAMIENTO_SERVICE_KEY` Cotización arranca normal, deja una advertencia en el log y el
    linaje responde 503 `LINAJE_NO_DISPONIBLE`. Así el orden de despliegue entre backend e infra
    no tumba la cotización.
    """
    e = os.environ if env is None else env
    url = (e.get("PERFILAMIENTO_URL") or "").strip()
    clave = (e.get("PERFILAMIENTO_SERVICE_KEY") or "").strip()
    faltantes = [n for n, v in (("PERFILAMIENTO_URL", url), ("PERFILAMIENTO_SERVICE_KEY", clave)) if not v]
    if faltantes:
        log.warning(
            "linaje_sin_configurar faltan=%s: el linaje del asesor responderá 503",
            ",".join(faltantes),
        )
        return LinajeSinConfigurar()
    return LinajeHttp(
        base_url=url,
        api_key=clave,
        timeout_s=float(e.get("PERFILAMIENTO_TIMEOUT_S", "1.0")),
    )
