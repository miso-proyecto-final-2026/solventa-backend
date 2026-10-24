"""Migración de Alembic del esquema `siniestros` contra PostgreSQL real (HU27).

Se omite sola si Docker no está disponible.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app.siniestros.modelos_db import SCHEMA, BaseSiniestros
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[2]
TABLAS = {
    "eventos_parametricos",
    "eventos_rechazados",
    "publicaciones_bus",
    "eventos_procesados",
}
SOLO_INSERCION = sorted(TABLAS - {"eventos_procesados"})


@pytest.fixture
def base_migrada(postgres_url, monkeypatch):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    monkeypatch.setenv("SINIESTROS_DATABASE_URL", postgres_url)
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
        diferencias = compare_metadata(contexto, BaseSiniestros.metadata)

    assert diferencias == []


@pytest.mark.parametrize("tabla", SOLO_INSERCION)
def test_las_tablas_son_de_solo_insercion(base_migrada, tabla):
    engine, _ = base_migrada
    with engine.begin() as c:
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.eventos_parametricos (id, evento_id, fuente, tipo, datos, "
                "ocurrido_en, recibido_en, estado_validacion) VALUES ('u1', 'e1', 'f', 'sismo', "
                "'{}', now(), now(), 'valido')"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.eventos_rechazados (id, fuente, codigo, detalles, recibido_en) "
                "VALUES ('r1', 'f', 'EVENTO_INVALIDO', '[]', now())"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.publicaciones_bus (evento_uuid, topic, publicado_en) "
                "VALUES ('u1', 't', now())"
            )
        )

    with pytest.raises(DBAPIError, match="solo inserción"), engine.begin() as c:
        c.execute(text(f"DELETE FROM {SCHEMA}.{tabla}"))


def test_la_migracion_es_reversible(base_migrada):
    engine, cfg = base_migrada

    command.downgrade(cfg, "base")

    assert not (TABLAS & set(inspect(engine).get_table_names(schema=SCHEMA)))


def test_el_mismo_evento_del_mismo_proveedor_no_se_registra_dos_veces(base_migrada):
    engine, _ = base_migrada
    insertar = (
        f"INSERT INTO {SCHEMA}.eventos_parametricos (id, evento_id, fuente, tipo, datos, "
        "ocurrido_en, recibido_en, estado_validacion) VALUES (:id, 'e1', 'f', 'sismo', "
        "'{}', now(), now(), 'valido')"
    )
    with engine.begin() as c:
        c.execute(text(insertar), {"id": "u1"})

    with pytest.raises(DBAPIError, match="uq_evento_fuente"), engine.begin() as c:
        c.execute(text(insertar), {"id": "u2"})


def test_eventos_procesados_admite_contar_reentregas(base_migrada):
    engine, _ = base_migrada
    with engine.begin() as c:
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.eventos_procesados (consumidor, event_id, procesado_en, reentregas) "
                "VALUES ('g', 'e1', now(), 0)"
            )
        )
        c.execute(text(f"UPDATE {SCHEMA}.eventos_procesados SET reentregas = reentregas + 1"))
        assert c.execute(text(f"SELECT reentregas FROM {SCHEMA}.eventos_procesados")).scalar() == 1
