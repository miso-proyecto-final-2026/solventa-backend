"""Consulta del consentimiento a MS Identidad (HU06) y stub para desarrollo local.

Cotización no importa código de Identidad ni lee sus tablas (ADR-0001): lo consulta por REST
síncrono (ADR-0002) con la clave de servicio.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

from app.proveedores.dominio import EstadoConsentimiento

logger = logging.getLogger("solventa.consentimiento")

TIPO_OPEN_FINANCE_OPEN_DATA = "OPEN_FINANCE_OPEN_DATA"


class ConsentimientoStub:
    """Solo para desarrollo local y pruebas, cuando no hay MS Identidad configurado."""

    def __init__(
        self,
        por_defecto: EstadoConsentimiento = EstadoConsentimiento.OTORGADO,
        estados: dict[str, EstadoConsentimiento] | None = None,
    ) -> None:
        self._por_defecto = por_defecto
        self._estados = estados or {}

    async def estado(self, cliente_id: str) -> EstadoConsentimiento:
        return self._estados.get(cliente_id, self._por_defecto)


class ConsentimientoHttp:
    """Consulta síncrona del consentimiento vigente en MS Identidad.

    Falla cerrado: ante un error, timeout o respuesta inesperada devuelve NO_OTORGADO. Sin
    consentimiento no se llama a proveedores y la cotización se calcula con el perfil por defecto.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 0.3,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = cliente or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s
        )
        self._cabeceras = {"X-Service-Key": api_key}

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConsentimientoHttp | None:
        e = os.environ if env is None else env
        url = e.get("IDENTIDAD_URL")
        if not url:
            return None
        return cls(
            base_url=url,
            api_key=e.get("IDENTIDAD_SERVICE_KEY", ""),
            timeout_s=float(e.get("IDENTIDAD_TIMEOUT_S", "0.3")),
        )

    async def estado(self, cliente_id: str) -> EstadoConsentimiento:
        ruta = f"/v1/clientes/{quote(cliente_id, safe='')}/consentimientos/{TIPO_OPEN_FINANCE_OPEN_DATA}"
        try:
            respuesta = await self._http.get(ruta, headers=self._cabeceras)
        except httpx.HTTPError as exc:
            logger.warning("identidad_no_disponible cliente=%s error=%s", cliente_id, type(exc).__name__)
            return EstadoConsentimiento.NO_OTORGADO
        if respuesta.status_code == 404:
            return EstadoConsentimiento.NO_OTORGADO
        if respuesta.status_code != 200:
            logger.warning("identidad_respuesta_inesperada status=%s", respuesta.status_code)
            return EstadoConsentimiento.NO_OTORGADO
        try:
            return EstadoConsentimiento(respuesta.json()["estado"])
        except (ValueError, KeyError, TypeError):
            logger.warning("identidad_cuerpo_invalido cliente=%s", cliente_id)
            return EstadoConsentimiento.NO_OTORGADO

    async def cerrar(self) -> None:
        await self._http.aclose()
