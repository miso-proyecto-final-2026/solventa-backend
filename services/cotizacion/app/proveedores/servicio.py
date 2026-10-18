"""Orquestación de la consulta de perfil: consentimiento → caché → proveedor → degradación."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.contexto import correlacion_id, cotizacion_en_curso
from app.proveedores.cache import CachePerfil
from app.proveedores.circuit_breaker import CircuitBreaker
from app.proveedores.config import ConfigProveedores
from app.proveedores.dominio import (
    CATEGORIAS_POR_FUENTE,
    PERFIL_POR_DEFECTO,
    ConsentimientoPort,
    EstadoConsentimiento,
    Fuente,
    PerfilCrudo,
    ProveedorError,
    RegistroLlamada,
    RegistroTrazabilidad,
    ResultadoConsulta,
    ResultadoFuente,
)
from app.proveedores.ttl import ttl_con_jitter

log = logging.getLogger(__name__)

# Clave reservada con que se guarda en caché la fecha de consulta al proveedor;
# nunca llega al payload que ven los normalizadores.
_META_OBTENIDO_EN = "__obtenido_en"


def _separar_meta(guardado: dict[str, Any]) -> tuple[dict[str, Any], datetime | None]:
    payload = {k: v for k, v in guardado.items() if k != _META_OBTENIDO_EN}
    crudo = guardado.get(_META_OBTENIDO_EN)
    try:
        return payload, datetime.fromisoformat(crudo) if crudo else None
    except (TypeError, ValueError):
        return payload, None


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
        reloj: Callable[[], datetime] | None = None,
        trazar: Callable[[RegistroTrazabilidad], None] | None = None,
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
        self._reloj = reloj or (lambda: datetime.now(UTC))
        # Debe ser no bloqueante y no lanzar: la escritura real ocurre fuera del camino crítico.
        self._trazar = trazar or (lambda _r: None)
        self._consentimiento_id: dict[str, str | None] = {}

    async def obtener_perfil(self, cliente_id: str) -> PerfilCrudo:
        verificacion = await self._consentimiento.verificar(cliente_id)
        if verificacion.estado is not EstadoConsentimiento.OTORGADO:
            # Sin consentimiento no hay ninguna llamada a proveedores (criterio 2), pero la
            # omisión también deja huella (HU11).
            for fuente in self._proveedores:
                self._huella(
                    cliente_id,
                    fuente,
                    verificacion.consentimiento_id,
                    ResultadoConsulta.SIN_CONSENTIMIENTO,
                    latencia_ms=0.0,
                    hit_cache=False,
                )
            return PerfilCrudo(cliente_id=cliente_id, fuentes=[], por_defecto=True)
        fuentes = await asyncio.gather(
            *(
                self._consultar(cliente_id, f, verificacion.consentimiento_id)
                for f in self._proveedores
            )
        )
        return PerfilCrudo(cliente_id=cliente_id, fuentes=list(fuentes))

    def conectar_trazabilidad(
        self, trazar: Callable[[RegistroTrazabilidad], None]
    ) -> None:
        """Registra el destino de la huella de cada consulta (lo arma el ensamblado)."""
        self._trazar = trazar

    def _huella(
        self,
        cliente_id: str,
        fuente: Fuente,
        consentimiento_id: str | None,
        resultado: ResultadoConsulta,
        latencia_ms: float,
        hit_cache: bool,
    ) -> None:
        try:
            self._trazar(
                RegistroTrazabilidad(
                    id=str(uuid.uuid4()),
                    cliente_id=cliente_id,
                    proveedor=fuente.value,
                    categorias=CATEGORIAS_POR_FUENTE.get(fuente, ()),
                    consultado_en=self._reloj(),
                    resultado=resultado,
                    latencia_ms=round(latencia_ms, 2),
                    hit_cache=hit_cache,
                    consentimiento_id=consentimiento_id,
                    cotizacion_id=cotizacion_en_curso.get(),
                    correlacion_id=correlacion_id.get(),
                )
            )
        except Exception:
            # La trazabilidad nunca debe tumbar la consulta (criterio 6).
            log.exception("no se pudo encolar el registro de trazabilidad")

    async def _consultar(
        self, cliente_id: str, fuente: Fuente, consentimiento_id: str | None = None
    ) -> ResultadoFuente:
        inicio = time.perf_counter()
        breaker = self.breakers[fuente]
        timeout = False

        vigente = await self._cache.obtener(cliente_id, fuente)
        if vigente is not None:
            payload, obtenido_en = _separar_meta(vigente)
            return self._cerrar(
                cliente_id,
                fuente,
                inicio,
                ResultadoFuente(
                    fuente, payload, hit_cache=True, obtenido_en=obtenido_en
                ),
                consentimiento_id,
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
                timeout = isinstance(exc, TimeoutError)
            else:
                breaker.registrar_exito()
                ttl = ttl_con_jitter(
                    self.config.ttl_s,
                    self.config.jitter_min,
                    self.config.jitter_max,
                    self._rng,
                )
                obtenido_en = self._reloj()
                await self._cache.guardar(
                    cliente_id,
                    fuente,
                    {**payload, _META_OBTENIDO_EN: obtenido_en.isoformat()},
                    ttl,
                    self.config.ttl_respaldo_s,
                )
                return self._cerrar(
                    cliente_id,
                    fuente,
                    inicio,
                    ResultadoFuente(fuente, payload, obtenido_en=obtenido_en),
                    consentimiento_id,
                )

        return self._cerrar(
            cliente_id,
            fuente,
            inicio,
            await self._degradar(cliente_id, fuente, timeout),
            consentimiento_id,
        )

    async def _degradar(
        self, cliente_id: str, fuente: Fuente, timeout: bool = False
    ) -> ResultadoFuente:
        respaldo = await self._cache.obtener_respaldo(cliente_id, fuente)
        if respaldo is not None:
            payload, obtenido_en = _separar_meta(respaldo)
            return ResultadoFuente(
                fuente,
                payload,
                degradado=True,
                hit_cache=True,
                obtenido_en=obtenido_en,
                timeout=timeout,
            )
        return ResultadoFuente(
            fuente,
            dict(PERFIL_POR_DEFECTO),
            degradado=True,
            por_defecto=True,
            timeout=timeout,
        )

    def _cerrar(
        self,
        cliente_id: str,
        fuente: Fuente,
        inicio: float,
        resultado: ResultadoFuente,
        consentimiento_id: str | None = None,
    ) -> ResultadoFuente:
        latencia_ms = (time.perf_counter() - inicio) * 1000
        self._registrar(
            RegistroLlamada(
                cliente_id=cliente_id,
                fuente=fuente,
                latencia_ms=latencia_ms,
                hit_cache=resultado.hit_cache,
                degradado=resultado.degradado,
                estado_circuito=self.breakers[fuente].estado,
            )
        )
        if not resultado.degradado:
            salida = ResultadoConsulta.OK
        elif resultado.timeout:
            salida = ResultadoConsulta.TIMEOUT
        else:
            salida = ResultadoConsulta.DEGRADADO
        self._huella(
            cliente_id,
            fuente,
            consentimiento_id,
            salida,
            latencia_ms,
            resultado.hit_cache and not resultado.degradado,
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

    async def cerrar(self) -> None:
        """Libera los recursos de red del adaptador de consentimiento, si los tiene."""
        cerrar = getattr(self._consentimiento, "cerrar", None)
        if cerrar is not None:
            await cerrar()

    async def ejecutar_sondeador(self) -> None:
        while True:
            await asyncio.sleep(self.config.intervalo_half_open_s)
            with contextlib.suppress(Exception):
                await self.sondear_una_vez()
