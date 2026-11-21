"""Oferta de vida hipotecario para el asesor (HU26): Cotización hace de fachada hacia MS Perfilamiento.

El precio lo calcula Perfilamiento (HU25, ADR-0024) con el perfil enriquecido del cliente. Cotización no
lo recalcula ni lo recibe del navegador: lo pide a Perfilamiento con su clave de servicio propia (la del
linaje, HU24) y lo registra como una cotización del producto `vida_hipotecario`, para que la suscripción
asistida (HU39) la use como cualquier otra cotización (ADR-0029, *a confirmar con el equipo*).

Como el linaje, se consulta por REST síncrono (ADR-0002), a nombre del asesor (`X-Consultado-Por`), sin
importar código de Perfilamiento ni leer sus tablas (ADR-0001).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from app.contexto import correlacion_id
from app.cotizacion.errores import (
    OfertaNoDisponible,
    OfertaSinConsentimiento,
    OfertaSinPerfil,
    SumaAseguradaFueraDeRango,
)
from app.logs import ganchos_http

log = logging.getLogger("solventa.oferta")

PRODUCTO_VIDA_HIPOTECARIO = "vida_hipotecario"
_CONSENTIMIENTO_NO_VIGENTE = "CONSENTIMIENTO_NO_VIGENTE"


class PasoPerfilamiento(BaseModel):
    paso: str
    nombre: str
    factor: str | None
    valor_perfil: float | int | None = None  # cifra del cliente: no sale hacia el asesor (ADR-0006)
    ajuste: int
    acumulado: int
    sin_dato: bool


class CoberturaPerfilamiento(BaseModel):
    tipo: str
    monto_cubierto: int
    deducible_porcentaje: str


class OfertaPerfilamiento(BaseModel):
    """La respuesta de `GET /v1/perfilamiento/{cliente_id}/oferta` (HU25), validada al recibirla."""

    oferta_id: str
    cliente_id: str
    producto: str
    moneda: str
    suma_asegurada: int
    prima_anual: int
    prima_mensual: int
    etapa_de_vida: str
    version_reglas: str
    perfil_id: str
    perfil_creado_en: datetime
    degradado: bool
    desglose: list[PasoPerfilamiento]
    coberturas: list[CoberturaPerfilamiento]
    creado_en: datetime


class OfertaPort(Protocol):
    async def enriquecer(self, cliente_id: str, asesor_id: str) -> None: ...

    async def calcular(
        self, cliente_id: str, asesor_id: str, suma_asegurada: int | None
    ) -> OfertaPerfilamiento: ...

    async def cerrar(self) -> None: ...


@dataclass(frozen=True)
class _Respuesta:
    estado: int
    cuerpo: Any


class OfertaHttp:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 1.0,
        timeout_enriquecer_s: float = 3.0,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = cliente or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, event_hooks=ganchos_http()
        )
        self._clave = api_key
        # Enriquecer consulta las fuentes externas (una lenta tarda hasta ~900 ms, HA02): más margen que leer.
        self._timeout_enriquecer = timeout_enriquecer_s

    def _cabeceras(self, asesor_id: str) -> dict[str, str]:
        cabeceras = {"X-Service-Key": self._clave, "X-Consultado-Por": asesor_id}
        if correlacion := correlacion_id.get():
            cabeceras["X-Correlation-Id"] = correlacion
        return cabeceras

    async def _pedir(self, metodo: str, ruta: str, asesor_id: str, **kwargs: Any) -> _Respuesta:
        try:
            r = await self._http.request(metodo, ruta, headers=self._cabeceras(asesor_id), **kwargs)
        except httpx.HTTPError as exc:
            log.warning("perfilamiento_no_disponible error=%s", type(exc).__name__)
            raise OfertaNoDisponible from exc
        try:
            cuerpo = r.json()
        except ValueError:
            cuerpo = None
        return _Respuesta(r.status_code, cuerpo)

    @staticmethod
    def _codigo(cuerpo: Any) -> str | None:
        if isinstance(cuerpo, dict) and isinstance(cuerpo.get("error"), dict):
            codigo = cuerpo["error"].get("code")
            return codigo if isinstance(codigo, str) else None
        return None

    def _rechazo(self, r: _Respuesta) -> None:
        """Traduce un 401/403: sin consentimiento es del cliente; lo demás, de configuración."""
        if r.estado == 403 and self._codigo(r.cuerpo) == _CONSENTIMIENTO_NO_VIGENTE:
            raise OfertaSinConsentimiento
        # Clave ausente o rotada: no es culpa del asesor; se ve en el log.
        log.error("perfilamiento_rechazo_la_clave status=%s", r.estado)
        raise OfertaNoDisponible

    async def enriquecer(self, cliente_id: str, asesor_id: str) -> None:
        ruta = f"/v1/perfilamiento/{quote(cliente_id, safe='')}/enriquecer"
        r = await self._pedir("POST", ruta, asesor_id, timeout=self._timeout_enriquecer)
        if r.estado in (200, 201):
            return
        if r.estado in (401, 403):
            self._rechazo(r)
        log.warning("perfilamiento_enriquecer_fallo status=%s", r.estado)
        raise OfertaNoDisponible

    async def calcular(
        self, cliente_id: str, asesor_id: str, suma_asegurada: int | None
    ) -> OfertaPerfilamiento:
        ruta = f"/v1/perfilamiento/{quote(cliente_id, safe='')}/oferta"
        params = {} if suma_asegurada is None else {"suma_asegurada": suma_asegurada}
        r = await self._pedir("GET", ruta, asesor_id, params=params)
        if r.estado == 404:
            raise OfertaSinPerfil
        if r.estado in (401, 403):
            self._rechazo(r)
        if r.estado == 422:
            raise SumaAseguradaFueraDeRango(_motivo_suma(r.cuerpo))
        if r.estado != 200:
            log.warning("perfilamiento_respuesta_inesperada status=%s", r.estado)
            raise OfertaNoDisponible
        try:
            oferta = OfertaPerfilamiento.model_validate(r.cuerpo)
        except ValidationError as exc:
            log.warning("perfilamiento_cuerpo_invalido")
            raise OfertaNoDisponible from exc
        if oferta.producto != PRODUCTO_VIDA_HIPOTECARIO or oferta.cliente_id != cliente_id:
            log.warning("perfilamiento_oferta_inesperada producto=%s", oferta.producto)
            raise OfertaNoDisponible
        return oferta

    async def cerrar(self) -> None:
        await self._http.aclose()


def _motivo_suma(cuerpo: Any) -> str:
    """El motivo que dio Perfilamiento para la suma (p. ej. los límites), o uno genérico."""
    if isinstance(cuerpo, dict) and isinstance(cuerpo.get("error"), dict):
        for detalle in cuerpo["error"].get("details") or []:
            if isinstance(detalle, dict) and detalle.get("campo") == "suma_asegurada":
                motivo = detalle.get("motivo")
                if isinstance(motivo, str) and motivo:
                    return motivo
    return "La suma asegurada está fuera de los límites del producto."


class OfertaSinConfigurar:
    """Sin URL o sin clave de Perfilamiento: la oferta responde «no disponible» (503)."""

    async def enriquecer(self, cliente_id: str, asesor_id: str) -> None:
        raise OfertaNoDisponible

    async def calcular(
        self, cliente_id: str, asesor_id: str, suma_asegurada: int | None
    ) -> OfertaPerfilamiento:
        raise OfertaNoDisponible

    async def cerrar(self) -> None:
        return None


def construir_oferta(env: dict[str, str] | None = None) -> OfertaPort:
    """Como el linaje (HU24): sin `PERFILAMIENTO_URL` o `PERFILAMIENTO_SERVICE_KEY`, Cotización arranca
    igual (HA01), avisa en el log y la oferta responde 503 `OFERTA_NO_DISPONIBLE`."""
    e = os.environ if env is None else env
    url = (e.get("PERFILAMIENTO_URL") or "").strip()
    clave = (e.get("PERFILAMIENTO_SERVICE_KEY") or "").strip()
    faltantes = [n for n, v in (("PERFILAMIENTO_URL", url), ("PERFILAMIENTO_SERVICE_KEY", clave)) if not v]
    if faltantes:
        log.warning(
            "oferta_sin_configurar faltan=%s: la oferta de vida hipotecario responderá 503",
            ",".join(faltantes),
        )
        return OfertaSinConfigurar()
    return OfertaHttp(
        base_url=url,
        api_key=clave,
        timeout_s=float(e.get("PERFILAMIENTO_TIMEOUT_S", "1.0")),
        timeout_enriquecer_s=float(e.get("PERFILAMIENTO_ENRIQUECER_TIMEOUT_S", "3.0")),
    )
