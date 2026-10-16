"""Migración de Alembic del esquema `cotizacion` contra PostgreSQL real (HU12).

Se omite sola si Docker no está disponible.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.cotizacion.modelos_db import SCHEMA, BaseCotizacion

pytestmark = pytest.mark.contenedores


def test_la_migracion_crea_el_esquema_y_las_tres_tablas(base_migrada):
    tablas = set(inspect(base_migrada).get_table_names(schema=SCHEMA))

    assert {"cotizaciones", "coberturas", "log_latencia"} <= tablas


def test_la_migracion_coincide_con_los_modelos_sin_deriva(base_migrada):
    with base_migrada.connect() as conn:
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
        diferencias = compare_metadata(contexto, BaseCotizacion.metadata)

    assert diferencias == []


def test_no_hay_llaves_foraneas_hacia_otros_esquemas(base_migrada):
    insp = inspect(base_migrada)
    for tabla in insp.get_table_names(schema=SCHEMA):
        for fk in insp.get_foreign_keys(tabla, schema=SCHEMA):
            assert fk["referred_schema"] == SCHEMA, (tabla, fk)
    columnas = {c["name"] for c in insp.get_columns("cotizaciones", schema=SCHEMA)}
    assert "cliente_id" in columnas  # el cliente se referencia por id
    assert not any(
        fk["referred_table"] == "clientes"
        for fk in insp.get_foreign_keys("cotizaciones", schema=SCHEMA)
    )


def test_la_migracion_se_puede_revertir(engine_pg, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", "base")

    assert inspect(engine_pg).get_table_names(schema=SCHEMA) == ["alembic_version"]
