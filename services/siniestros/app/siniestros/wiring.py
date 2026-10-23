"""Ensamblado de Siniestros desde la configuración del entorno."""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.configuracion import permitir_sustituto_local
from app.siniestros.bus import TOPIC_EVENTOS, BusMemoria, Consumidor
from app.siniestros.config import SQLITE_LOCAL, ConfigSiniestros
from app.siniestros.repositorio import (
    RepositorioEventos,
    crear_engine,
    crear_tablas_locales,
)
from app.siniestros.servicio import (
    CONSUMIDOR_RECEPCION,
    Receptor,
    RegistradorProcesamiento,
)


def construir_receptor(
    config: ConfigSiniestros | None = None,
) -> tuple[Receptor, Consumidor, Engine]:
    config = config or ConfigSiniestros.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local(
            "SINIESTROS_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})"
        )
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    repositorio = RepositorioEventos(sessionmaker(engine, expire_on_commit=False))
    bus = BusMemoria(config.particiones, config.capacidad_rezago)
    bus.registrar_grupo(TOPIC_EVENTOS, CONSUMIDOR_RECEPCION)
    consumidor = Consumidor(
        bus,
        TOPIC_EVENTOS,
        CONSUMIDOR_RECEPCION,
        RegistradorProcesamiento(repositorio),
        reintentos=config.reintentos_consumidor,
        espera_base_s=config.espera_base_consumidor_s,
    )
    return Receptor(repositorio, bus), consumidor, engine
