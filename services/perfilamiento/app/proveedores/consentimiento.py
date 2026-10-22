"""Consulta del consentimiento a MS Identidad (HU06) y stub para desarrollo local.

Perfilamiento no importa código de Identidad ni lee sus tablas (ADR-0001): lo consulta por REST
síncrono (ADR-0002, conector de E05) con la clave de servicio.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from app.proveedores.dominio import EstadoConsentimiento, VerificacionConsentimiento

log = logging.getLogger("solventa.consentimiento")

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

    async def verificar(self, cliente_id: str) -> VerificacionConsentimiento:
        estado = self._estados.get(cliente_id, self._por_defecto)
        referencia = (
            f"stub-{cliente_id}" if estado is EstadoConsentimiento.OTORGADO else None
        )
        return VerificacionConsentimiento(estado, referencia)

    async def estado(self, cliente_id: str) -> EstadoConsentimiento:
        return (await self.verificar(cliente_id)).estado


class ConsentimientoHttp:
    """Consulta síncrona del consentimiento vigente en MS Identidad.

    Falla cerrado: ante un error, timeout o respuesta inesperada devuelve NO_OTORGADO. Sin
    consentimiento no se llama a proveedores y el perfilamiento se bloquea (el intento queda registrado).

    Como toda dependencia del camino crítico (HA01), va con timeout duro y circuit breaker: tras
    `umbral_fallos` fallos seguidos el circuito se abre y, durante `enfriamiento_s`, responde el
    valor por defecto sin llamar a Identidad; luego deja pasar un intento de prueba.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 0.3,
        cliente: httpx.AsyncClient | None = None,
        umbral_fallos: int = 5,
        enfriamiento_s: float = 15.0,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = cliente or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s
        )
        self._cabeceras = {"X-Service-Key": api_key}
        self._umbral = umbral_fallos
        self._enfriamiento = enfriamiento_s
        self._reloj = reloj
        self._fallos = 0
        self._abierto_desde: float | None = None

    @classmethod
    def desde_entorno(
        cls, env: dict[str, str] | None = None
    ) -> ConsentimientoHttp | None:
        e = os.environ if env is None else env
        url = e.get("IDENTIDAD_URL")
        if not url:
            return None
        return cls(
            base_url=url,
            api_key=e.get("IDENTIDAD_SERVICE_KEY", ""),
            timeout_s=float(e.get("IDENTIDAD_TIMEOUT_S", "0.3")),
            umbral_fallos=int(e.get("IDENTIDAD_UMBRAL_FALLOS", "5")),
            enfriamiento_s=float(e.get("IDENTIDAD_ENFRIAMIENTO_S", "15")),
        )

    @property
    def circuito_abierto(self) -> bool:
        return self._abierto_desde is not None

    def _permite_llamada(self) -> bool:
        if self._abierto_desde is None:
            return True
        if self._reloj() - self._abierto_desde >= self._enfriamiento:
            # Intento de prueba: si falla, el circuito se reabre de inmediato.
            self._abierto_desde = None
            self._fallos = self._umbral - 1
            return True
        return False

    def _registrar_fallo(self) -> None:
        self._fallos += 1
        if self._fallos >= self._umbral:
            self._abierto_desde = self._reloj()

    def _registrar_exito(self) -> None:
        self._fallos = 0
        self._abierto_desde = None

    async def estado(self, cliente_id: str) -> EstadoConsentimiento:
        return (await self.verificar(cliente_id)).estado

    async def verificar(self, cliente_id: str) -> VerificacionConsentimiento:
        """Estado vigente y, si Identidad lo informa, el id del consentimiento verificado."""
        no_otorgado = VerificacionConsentimiento(EstadoConsentimiento.NO_OTORGADO)
        if not self._permite_llamada():
            return no_otorgado
        ruta = (
            f"/v1/clientes/{quote(cliente_id, safe='')}"
            f"/consentimientos/{TIPO_OPEN_FINANCE_OPEN_DATA}"
        )
        try:
            respuesta = await self._http.get(ruta, headers=self._cabeceras)
        except httpx.HTTPError as exc:
            self._registrar_fallo()
            log.warning("identidad_no_disponible error=%s", type(exc).__name__)
            return no_otorgado
        if respuesta.status_code >= 500:
            self._registrar_fallo()
            log.warning("identidad_respuesta_5xx status=%s", respuesta.status_code)
            return no_otorgado
        # Identidad respondió: el servicio está sano aunque la respuesta sea 404 o 401.
        self._registrar_exito()
        if respuesta.status_code == 404:
            return no_otorgado
        if respuesta.status_code != 200:
            log.warning(
                "identidad_respuesta_inesperada status=%s", respuesta.status_code
            )
            return no_otorgado
        try:
            cuerpo = respuesta.json()
            estado = EstadoConsentimiento(cuerpo["estado"])
            referencia = cuerpo.get("id")
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("identidad_cuerpo_invalido")
            return no_otorgado
        if not isinstance(referencia, str) or not referencia:
            referencia = None
        return VerificacionConsentimiento(estado, referencia)

    async def cerrar(self) -> None:
        await self._http.aclose()
