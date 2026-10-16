"""Entorno de Alembic para el esquema `cotizacion` (cada microservicio versiona su esquema)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.cotizacion.config import ConfigCotizacion
from app.cotizacion.modelos_db import SCHEMA, BaseCotizacion
from app.cotizacion.repositorio import normalizar_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = BaseCotizacion.metadata


def _incluir(name, type_, parent_names):
    # Solo el esquema de Cotización; las demás tablas pertenecen a otros contextos.
    return name == SCHEMA if type_ == "schema" else True


def run_migrations_online() -> None:
    url = normalizar_url(ConfigCotizacion.desde_entorno().database_url)
    if url.startswith("sqlite"):
        raise RuntimeError(
            "Las migraciones requieren PostgreSQL (esquema 'cotizacion')."
        )
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        # El esquema debe existir antes de crear la tabla de versiones de Alembic.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
            include_name=_incluir,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError(
        "Modo offline no soportado; use 'alembic upgrade head' con una base."
    )
run_migrations_online()
