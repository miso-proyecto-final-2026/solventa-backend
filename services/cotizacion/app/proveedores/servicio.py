"""Orquestación de la consulta de perfil: consentimiento → caché → proveedor → degradación."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from app.proveedores.cache import CachePerfil
from app.proveedores.circuit_breaker import CircuitBreaker
from app.proveedores.config import ConfigProveedores
from app.proveedores.dominio import (
    PERFIL_POR_DEFECTO,
    ConsentimientoPort,
    EstadoConsentimiento,
    Fuente,
    PerfilCrudo,
    ProveedorError,
    RegistroLlamada,
    ResultadoFuente,
)
from app.proveedores.ttl import ttl_con_jitter

log = logging.getLogger(__name__)


class ServicioPerfil:
    def __init__(
        self,
        proveedores: dict[Fuente, Any],
        cache: CachePerfil,
        consentimiento: ConsentimientoPort,
        config: ConfigProveedores | None = None,
        breakers: dict[Fuente, CircuitBreaker] | None = None,
        registrar: Callable[[RegistroLlamada], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._proveedores = proveedores
        self._cache = cache
        self._consentimiento = consentimiento
        self.config = config or ConfigProveedores()
        self.breakers = breakers or {
            f: CircuitBreaker(self.config.umbral_fallos) for f in proveedores
        }
        self._registrar = registrar or (lambda _r: None)
        self._rng = rng or random.Random()

    async def obtener_perfil(self, cliente_id: str) -> PerfilCrudo:
        if (
            await self._consentimiento.estado(cliente_id)
            is not EstadoConsentimiento.OTORGADO
        ):
            # Sin consentimiento no hay ninguna llamada a proveedores (criterio 2).
            return PerfilCrudo(cliente_id=cliente_id, fuentes=[], por_defecto=True)
        fuentes = await asyncio.gather(
            *(self._consultar(cliente_id, f) for f in self._proveedores)
        )
        return PerfilCrudo(cliente_id=cliente_id, fuentes=list(fuentes))

    async def _consultar(self, cliente_id: str, fuente: Fuente) -> ResultadoFuente:
        inicio = time.perf_counter()
        breaker = self.breakers[fuente]

        vigente = await self._cache.obtener(cliente_id, fuente)
        if vigente is not None:
            return self._cerrar(
                cliente_id,
                fuente,
                inicio,
                ResultadoFuente(fuente, vigente, hit_cache=True),
            )

        if breaker.permite_solicitud():
            try:
                payload = await asyncio.wait_for(
                    self._proveedores[fuente].consultar_perfil(cliente_id),
                    timeout=self.config.timeout_s,
                )
                if not payload:
                    raise ProveedorError("respuesta vacía")
            except (TimeoutError, ProveedorError, OSError) as exc:
                log.warning(
                    "proveedor %s degradado: %s", fuente.value, type(exc).__name__
                )
                breaker.registrar_fallo()
            else:
                breaker.registrar_exito()
                ttl = ttl_con_jitter(
                    self.config.ttl_s,
                    self.config.jitter_min,
                    self.config.jitter_max,
                    self._rng,
                )
                await self._cache.guardar(
                    cliente_id, fuente, payload, ttl, self.config.ttl_respaldo_s
                )
                return self._cerrar(
                    cliente_id, fuente, inicio, ResultadoFuente(fuente, payload)
                )

        return self._cerrar(
            cliente_id, fuente, inicio, await self._degradar(cliente_id, fuente)
        )

    async def _degradar(self, cliente_id: str, fuente: Fuente) -> ResultadoFuente:
        respaldo = await self._cache.obtener_respaldo(cliente_id, fuente)
        if respaldo is not None:
            return ResultadoFuente(fuente, respaldo, degradado=True, hit_cache=True)
        return ResultadoFuente(
            fuente, dict(PERFIL_POR_DEFECTO), degradado=True, por_defecto=True
        )

    def _cerrar(
        self, cliente_id: str, fuente: Fuente, inicio: float, resultado: ResultadoFuente
    ) -> ResultadoFuente:
        self._registrar(
            RegistroLlamada(
                cliente_id=cliente_id,
                fuente=fuente,
                latencia_ms=(time.perf_counter() - inicio) * 1000,
                hit_cache=resultado.hit_cache,
                degradado=resultado.degradado,
                estado_circuito=self.breakers[fuente].estado,
            )
        )
        return resultado

    async def sondear_una_vez(self) -> None:
        """Sondeo half-open en segundo plano; nunca ocurre dentro de una solicitud."""
        for fuente, breaker in self.breakers.items():
            if not breaker.iniciar_sondeo():
                continue
            try:
                await asyncio.wait_for(
                    self._proveedores[fuente].sondear(), timeout=self.config.timeout_s
                )
                breaker.resultado_sondeo(exito=True)
            except (TimeoutError, ProveedorError, OSError):
                breaker.resultado_sondeo(exito=False)

    async def ejecutar_sondeador(self) -> None:
        while True:
            await asyncio.sleep(self.config.intervalo_half_open_s)
            with contextlib.suppress(Exception):
                await self.sondear_una_vez()
