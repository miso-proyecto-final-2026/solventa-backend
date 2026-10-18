"""Ensamblado del servicio de cotización desde la configuración del entorno."""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.cotizacion.config import ConfigCotizacion
from app.cotizacion.perfil import AdaptadorPerfilProveedores, PerfilPort
from app.cotizacion.reglas import cargar_reglas
from app.cotizacion.repositorio import (
    RepositorioCotizaciones,
    RepositorioTrazabilidad,
    crear_engine,
    crear_tablas_locales,
)
from app.cotizacion.servicio import ServicioCotizacion
from app.cotizacion.trazabilidad import EscritorTrazabilidad
from app.proveedores.servicio import ServicioPerfil


def construir_servicio_cotizacion(
    servicio_perfil: ServicioPerfil,
    config: ConfigCotizacion | None = None,
    perfiles: PerfilPort | None = None,
) -> tuple[ServicioCotizacion, Engine, EscritorTrazabilidad]:
    """Falla al arrancar si las reglas no existen o son inválidas (fail-fast).

    Devuelve también el escritor de trazabilidad (HU11): quien arranca la app debe
    llamar a `iniciar()` y, al apagar, a `detener()` para no perder lo pendiente.
    """
    config = config or ConfigCotizacion.desde_entorno()
    reglas = cargar_reglas(config.reglas_version, config.reglas_dir)
    engine = crear_engine(config.database_url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    fabrica = sessionmaker(engine, expire_on_commit=False)
    escritor = EscritorTrazabilidad(
        RepositorioTrazabilidad(fabrica),
        capacidad=config.traza_cola_max,
        reintentos=config.traza_reintentos,
    )
    servicio_perfil.conectar_trazabilidad(escritor.encolar)
    servicio = ServicioCotizacion(
        reglas,
        perfiles or AdaptadorPerfilProveedores(servicio_perfil),
        RepositorioCotizaciones(fabrica),
        trazabilidad=RepositorioTrazabilidad(fabrica),
    )
    return servicio, engine, escritor
