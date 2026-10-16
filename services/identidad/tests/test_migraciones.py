"""Migración de Alembic del esquema `identidad` contra PostgreSQL real (HU06).

Se omite sola si Docker no está disponible.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app import models  # noqa: F401
from app.db import SCHEMA, Base

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[1]


def _docker_disponible() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001  (cualquier fallo de Docker = omitir la prueba)
        return False


@pytest.fixture(scope="module")
def postgres_url():
    if not _docker_disponible():
        pytest.skip("Docker no disponible: se omiten las pruebas con contenedores")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture
def engine_pg(postgres_url):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        c.execute(text("DROP TABLE IF EXISTS public.clientes"))
    yield engine
    engine.dispose()


@pytest.fixture
def alembic(postgres_url, monkeypatch):
    def _ejecutar(accion: str, destino: str) -> None:
        monkeypatch.setenv("DATABASE_URL", postgres_url)
        cfg = Config(str(RAIZ / "alembic.ini"))
        cfg.set_main_option("script_location", str(RAIZ / "migrations"))
        getattr(command, accion)(cfg, destino)

    return _ejecutar


@pytest.fixture
def base_migrada(engine_pg, alembic):
    alembic("upgrade", "head")
    return engine_pg


def test_la_migracion_crea_el_esquema_y_las_tablas(base_migrada):
    tablas = set(inspect(base_migrada).get_table_names(schema=SCHEMA))

    assert {
        "clientes",
        "sesiones",
        "consentimientos",
        "consentimiento_registros",
    } <= tablas


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
        diferencias = compare_metadata(contexto, Base.metadata)

    assert diferencias == []


def test_no_hay_llaves_foraneas_hacia_otros_esquemas(base_migrada):
    insp = inspect(base_migrada)
    for tabla in insp.get_table_names(schema=SCHEMA):
        for fk in insp.get_foreign_keys(tabla, schema=SCHEMA):
            assert fk["referred_schema"] == SCHEMA, (tabla, fk)


def test_no_puede_haber_dos_consentimientos_del_mismo_tipo_por_cliente(base_migrada):
    with base_migrada.begin() as c:
        _insertar_cliente(c, "c1")
        _insertar_consentimiento(c, "k1", "c1")
    with pytest.raises(DBAPIError), base_migrada.begin() as c:
        _insertar_consentimiento(c, "k2", "c1")


def test_el_registro_de_consentimiento_es_inmutable_en_la_base(base_migrada):
    with base_migrada.begin() as c:
        _insertar_cliente(c, "c1")
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.consentimiento_registros "
                "(id, cliente_id, tipo, evento, version_texto, canal, fecha) "
                "VALUES ('r1', 'c1', 'OPEN_FINANCE_OPEN_DATA', 'OTORGADO', 'v1', 'MOVIL', now())"
            )
        )

    with pytest.raises(DBAPIError, match="inmutable"), base_migrada.begin() as c:
        c.execute(text(f"UPDATE {SCHEMA}.consentimiento_registros SET version_texto = 'v2'"))
    with pytest.raises(DBAPIError, match="inmutable"), base_migrada.begin() as c:
        c.execute(text(f"DELETE FROM {SCHEMA}.consentimiento_registros"))


def test_la_migracion_copia_los_clientes_que_vivian_en_public(engine_pg, alembic):
    with engine_pg.begin() as c:
        c.execute(
            text(
                "CREATE TABLE public.clientes (id varchar(36), nombre varchar(200), "
                "apellido varchar(200), tipo_documento varchar(20), numero_documento varchar(20), "
                "email varchar(320), celular varchar(20), password_hash varchar(200), "
                "estado_kyc varchar(20), creado_en timestamptz, actualizado_en timestamptz)"
            )
        )
        c.execute(
            text(
                "INSERT INTO public.clientes VALUES ('viejo', 'Ana', 'Pérez', 'CC', '1', "
                "'a@example.com', '3001234567', 'hash', 'PENDIENTE', now(), now())"
            )
        )

    alembic("upgrade", "head")

    with engine_pg.connect() as c:
        ids = c.execute(text(f"SELECT id FROM {SCHEMA}.clientes")).scalars().all()
    assert ids == ["viejo"]


def test_la_migracion_no_falla_si_el_rol_no_puede_leer_public_clientes(engine_pg, postgres_url):
    """En staging public.clientes es del rol del servicio anterior: el rol de Identidad no la lee."""
    with engine_pg.begin() as c:
        c.execute(text("CREATE TABLE public.clientes (id varchar(36), nombre varchar(200))"))
        c.execute(text("INSERT INTO public.clientes VALUES ('viejo', 'Ana')"))
        c.execute(text("DROP ROLE IF EXISTS ident_sin_permiso"))
        c.execute(text("CREATE ROLE ident_sin_permiso LOGIN PASSWORD 'ident_sin_permiso'"))
        base = c.execute(text("SELECT current_database()")).scalar_one()
        c.execute(text(f'GRANT CREATE ON DATABASE "{base}" TO ident_sin_permiso'))

    url = engine_pg.url.set(username="ident_sin_permiso", password="ident_sin_permiso")
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    previo = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        command.upgrade(cfg, "head")
        with engine_pg.connect() as c:
            copiados = c.execute(text(f"SELECT count(*) FROM {SCHEMA}.clientes")).scalar_one()
        assert copiados == 0  # sin permiso no se copia nada, pero la migración termina
        assert "sesiones" in inspect(engine_pg).get_table_names(schema=SCHEMA)
    finally:
        if previo is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previo
        with engine_pg.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
            c.execute(text("DROP OWNED BY ident_sin_permiso"))
            c.execute(text("DROP ROLE ident_sin_permiso"))

def test_la_migracion_se_puede_revertir(engine_pg, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", "base")

    assert inspect(engine_pg).get_table_names(schema=SCHEMA) == ["alembic_version"]


def _insertar_cliente(c, id_):
    c.execute(
        text(
            f"INSERT INTO {SCHEMA}.clientes (id, nombre, apellido, tipo_documento, "
            "numero_documento, email, celular, password_hash, estado_kyc, creado_en, actualizado_en) "
            f"VALUES ('{id_}', 'Ana', 'Pérez', 'CC', '{id_}', '{id_}@example.com', '3001234567', "
            "'hash', 'PENDIENTE', now(), now())"
        )
    )


def _insertar_consentimiento(c, id_, cliente_id):
    c.execute(
        text(
            f"INSERT INTO {SCHEMA}.consentimientos (id, cliente_id, tipo, estado, version_texto, "
            f"fecha_otorgamiento) VALUES ('{id_}', '{cliente_id}', 'OPEN_FINANCE_OPEN_DATA', "
            "'OTORGADO', 'v1', now())"
        )
    )
