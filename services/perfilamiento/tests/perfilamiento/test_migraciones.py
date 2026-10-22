"""Migración de Alembic del esquema `perfilamiento` contra PostgreSQL real (HU23).

Se omite sola si Docker no está disponible.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app.perfilamiento.modelos_db import SCHEMA, BasePerfilamiento

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[2]
TABLAS = {"perfiles_enriquecidos", "linaje_fuentes", "intentos_bloqueados"}


@pytest.fixture
def base_migrada(postgres_url, monkeypatch):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    monkeypatch.setenv("PERFILAMIENTO_DATABASE_URL", postgres_url)
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(cfg, "head")
    yield engine, cfg
    engine.dispose()


def test_la_migracion_crea_el_esquema_y_sus_tablas(base_migrada):
    engine, _ = base_migrada

    assert TABLAS <= set(inspect(engine).get_table_names(schema=SCHEMA))


def test_la_migracion_coincide_con_los_modelos_sin_deriva(base_migrada):
    engine, _ = base_migrada
    with engine.connect() as conn:
        contexto = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "version_table_schema": SCHEMA,
                "include_name": lambda nombre, tipo, _p: (
                    nombre == SCHEMA if tipo == "schema" else True
                ),
            },
        )
        diferencias = compare_metadata(contexto, BasePerfilamiento.metadata)

    assert diferencias == []


@pytest.mark.parametrize("tabla", sorted(TABLAS))
def test_las_tablas_son_de_solo_insercion(base_migrada, tabla):
    engine, _ = base_migrada
    with engine.begin() as c:
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.perfiles_enriquecidos (id, cliente_id, perfil, completitud, "
                "degradado, creado_en) VALUES ('p1', 'c1', '{}', 1, false, now())"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.linaje_fuentes (perfil_id, fuente, campos, hit_cache, "
                "degradado, por_defecto) VALUES ('p1', 'open_data', '[]', false, false, false)"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.intentos_bloqueados (id, cliente_id, operacion, origen, "
                "estado_consentimiento, ocurrido_en) VALUES ('i1', 'c1', 'ENRIQUECER', 'x', "
                "'REVOCADO', now())"
            )
        )

    with pytest.raises(DBAPIError, match="solo inserción"), engine.begin() as c:
        c.execute(text(f"DELETE FROM {SCHEMA}.{tabla}"))


def test_la_migracion_es_reversible(base_migrada):
    engine, cfg = base_migrada

    command.downgrade(cfg, "base")

    assert not (TABLAS & set(inspect(engine).get_table_names(schema=SCHEMA)))
