"""Fachada de cotización: validar -> perfil -> rating -> persistir -> medir (HU12).

La latencia interna (`latencia_interna_ms`) cubre lectura de perfil/caché,
rating y persistencia de la cotización. El registro de latencia se escribe
después, en una transacción aparte, y no cuenta dentro de ese tiempo; si falla
no se tumba la cotización (es telemetría).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.cotizacion.dominio import (
    ESTADO_EMITIDA,
    CotizacionEmitida,
    SolicitudCotizacion,
)
from app.cotizacion.errores import CotizacionNoEncontrada
from app.cotizacion.motor import calcular_prima, validar_solicitud
from app.cotizacion.perfil import PerfilPort
from app.cotizacion.reglas import ReglasTarifarias
from app.cotizacion.repositorio import RepositorioCotizaciones

log = logging.getLogger("cotizacion.latencia")


def _ahora() -> datetime:
    return datetime.now(UTC)


def _nuevo_id() -> str:
    return str(uuid.uuid4())


class ServicioCotizacion:
    def __init__(
        self,
        reglas: ReglasTarifarias,
        perfiles: PerfilPort,
        repositorio: RepositorioCotizaciones,
        reloj: Callable[[], datetime] = _ahora,
        generar_id: Callable[[], str] = _nuevo_id,
        cronometro: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._reglas = reglas
        self._perfiles = perfiles
        self._repo = repositorio
        self._reloj = reloj
        self._generar_id = generar_id
        self._cronometro = cronometro

    async def cotizar(self, solicitud: SolicitudCotizacion) -> CotizacionEmitida:
        inicio = self._cronometro()
        # Se valida antes de cualquier consulta externa: un dato inválido no cuesta I/O.
        validar_solicitud(solicitud.producto, solicitud.datos, self._reglas)

        perfil = await self._perfiles.obtener(solicitud.cliente_id)
        resultado = calcular_prima(
            solicitud.producto, solicitud.datos, perfil, self._reglas
        )

        ahora = self._reloj()
        emitida = CotizacionEmitida(
            id=self._generar_id(),
            cliente_id=solicitud.cliente_id,
            producto=resultado.producto,
            estado=ESTADO_EMITIDA,
            moneda=resultado.moneda,
            prima_anual=resultado.prima_anual,
            prima_mensual=resultado.prima_mensual,
            coberturas=resultado.coberturas,
            componentes=resultado.componentes,
            version_reglas=resultado.version_reglas,
            degradado=resultado.degradado,
            aproximada=resultado.aproximada,
            vigente_hasta=ahora + timedelta(days=resultado.vigencia_dias),
            creada_en=ahora,
        )
        await asyncio.to_thread(self._repo.guardar, emitida, dict(solicitud.datos))

        latencia_ms = round((self._cronometro() - inicio) * 1000, 2)
        emitida = replace(
            emitida, hit_cache=perfil.hit_cache, latencia_interna_ms=latencia_ms
        )
        await self._registrar_latencia(emitida)
        return emitida

    async def _registrar_latencia(self, c: CotizacionEmitida) -> None:
        log.info(
            "producto=%s latencia_interna_ms=%.1f hit_cache=%s degradado=%s",
            c.producto,
            c.latencia_interna_ms,
            c.hit_cache,
            c.degradado,
        )
        try:
            await asyncio.to_thread(
                self._repo.guardar_latencia,
                c.id,
                c.latencia_interna_ms,
                c.hit_cache,
                c.degradado,
                self._reloj(),
            )
        except Exception:
            log.exception("no se pudo registrar la latencia de la cotización %s", c.id)

    async def obtener(self, cotizacion_id: str) -> CotizacionEmitida:
        cotizacion = await asyncio.to_thread(self._repo.obtener, cotizacion_id)
        if cotizacion is None:
            raise CotizacionNoEncontrada(cotizacion_id)
        return cotizacion

    async def registrar_latencia_cliente(self, cotizacion_id: str, ms: float) -> None:
        await asyncio.to_thread(
            self._repo.registrar_latencia_cliente, cotizacion_id, ms
        )
