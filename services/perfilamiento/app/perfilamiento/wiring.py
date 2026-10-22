"""Ensamblado de Perfilamiento desde la configuración del entorno."""

from __future__ import annotations

import logging

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.configuracion import permitir_sustituto_local
from app.perfilamiento.config import SQLITE_LOCAL, ConfigPerfilamiento
from app.perfilamiento.repositorio import (
    RepositorioPerfiles,
    crear_engine,
    crear_tablas_locales,
)
from app.perfilamiento.servicio import Enriquecedor
from app.proveedores.cache import CacheMemoria, CachePerfil, CacheRedis
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoHttp, ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento, Fuente, RegistroLlamada
from app.proveedores.servicio import ServicioFuentes
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


def construir_fuentes(config: ConfigProveedores | None = None) -> ServicioFuentes:
    config = config or ConfigProveedores.desde_entorno()
    cache: CachePerfil
    if config.redis_url:
        from redis.asyncio import from_url

        cache = CacheRedis(from_url(config.redis_url))
    else:
        permitir_sustituto_local("REDIS_URL", "una caché en memoria por proceso")
        cache = CacheMemoria()
    consentimiento = ConsentimientoHttp.desde_entorno()
    if consentimiento is None:
        permitir_sustituto_local("IDENTIDAD_URL", "el stub de consentimiento (siempre otorgado)")
        consentimiento = ConsentimientoStub(EstadoConsentimiento.OTORGADO)
    return ServicioFuentes(
        proveedores={
            Fuente.OPEN_FINANCE: SimuladorOpenFinance(ConfigSimulador.desde_entorno("OPEN_FINANCE_SIM")),
            Fuente.OPEN_DATA: SimuladorOpenData(ConfigSimulador.desde_entorno("OPEN_DATA_SIM")),
        },
        cache=cache,
        consentimiento=consentimiento,
        config=config,
        registrar=_registrar,
    )


def construir_enriquecedor(
    fuentes: ServicioFuentes, config: ConfigPerfilamiento | None = None
) -> tuple[Enriquecedor, Engine]:
    config = config or ConfigPerfilamiento.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local(
            "PERFILAMIENTO_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})"
        )
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    fabrica = sessionmaker(engine, expire_on_commit=False)
    return Enriquecedor(fuentes, RepositorioPerfiles(fabrica)), engine
