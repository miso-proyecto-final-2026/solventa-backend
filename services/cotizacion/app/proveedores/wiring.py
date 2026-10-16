"""Ensamblado del servicio de perfiles desde la configuración del entorno."""

from __future__ import annotations

import logging

from app.proveedores.cache import CacheMemoria, CachePerfil, CacheRedis
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento, Fuente, RegistroLlamada
from app.proveedores.servicio import ServicioPerfil
from app.proveedores.simuladores import (
    ConfigSimulador,
    SimuladorOpenData,
    SimuladorOpenFinance,
)

log = logging.getLogger("proveedores.llamadas")


def _registrar(r: RegistroLlamada) -> None:
    log.info(
        "fuente=%s latencia_ms=%.1f hit_cache=%s degradado=%s circuito=%s",
        r.fuente.value,
        r.latencia_ms,
        r.hit_cache,
        r.degradado,
        r.estado_circuito.value,
    )


def construir_servicio(config: ConfigProveedores | None = None) -> ServicioPerfil:
    config = config or ConfigProveedores.desde_entorno()
    cache: CachePerfil
    if config.redis_url:
        from redis.asyncio import from_url

        cache = CacheRedis(from_url(config.redis_url))
    else:
        cache = CacheMemoria()
    return ServicioPerfil(
        proveedores={
            Fuente.OPEN_FINANCE: SimuladorOpenFinance(
                ConfigSimulador.desde_entorno("OPEN_FINANCE_SIM")
            ),
            Fuente.OPEN_DATA: SimuladorOpenData(
                ConfigSimulador.desde_entorno("OPEN_DATA_SIM")
            ),
        },
        cache=cache,
        consentimiento=ConsentimientoStub(EstadoConsentimiento.OTORGADO),
        config=config,
        registrar=_registrar,
    )
