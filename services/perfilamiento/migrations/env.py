"""Entorno de Alembic para el esquema `perfilamiento` (cada microservicio versiona su esquema)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.perfilamiento.config import ConfigPerfilamiento
from app.perfilamiento.modelos_db import SCHEMA, BasePerfilamiento
from app.perfilamiento.repositorio import normalizar_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = BasePerfilamiento.metadata


def _incluir(name, type_, parent_names):
    # Solo el esquema de Perfilamiento; las demás tablas pertenecen a otros contextos.
    return name == SCHEMA if type_ == "schema" else True


def run_migrations_online() -> None:
    url = ConfigPerfilamiento.desde_entorno().database_url
    if url is None:
        raise RuntimeError(
            "Defina PERFILAMIENTO_DATABASE_URL (o DATABASE_URL) con la URL de PostgreSQL."
        )
    url = normalizar_url(url)
    if url.startswith("sqlite"):
        raise RuntimeError("Las migraciones requieren PostgreSQL (esquema 'perfilamiento').")
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
    raise RuntimeError("Modo offline no soportado; use 'alembic upgrade head' con una base.")
run_migrations_online()
