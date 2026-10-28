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
    # Permite correr estas pruebas sin Docker apuntando a un PostgreSQL desechable ya levantado.
    existente = os.environ.get("TEST_POSTGRES_URL")
    if existente:
        yield existente
        return
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
        "verificaciones_kyc",
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
        _crear_public_clientes(c)

    alembic("upgrade", "head")

    with engine_pg.connect() as c:
        ids = c.execute(text(f"SELECT id FROM {SCHEMA}.clientes")).scalars().all()
    assert ids == ["viejo"]


@pytest.mark.parametrize("sin_usage_en_public", [False, True], ids=["sin_select_en_tabla", "sin_usage_en_esquema"])
def test_la_migracion_no_falla_si_el_rol_no_puede_leer_public_clientes(engine_pg, postgres_url, sin_usage_en_public):
    """En staging public.clientes es del rol del servicio anterior: el rol de Identidad no la lee.

    En staging además db-access quita el USAGE del esquema public a los demás roles: ahí falla hasta
    `to_regclass('public.clientes')` con "permission denied for schema public".
    """
    with engine_pg.begin() as c:
        # Debe tener las columnas reales: con menos, PostgreSQL falla al analizar el SELECT
        # (UndefinedColumn) antes de comprobar permisos y no se ejercita el caso de staging.
        _crear_public_clientes(c)
        c.execute(
            text(
                "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ident_sin_permiso') "
                "THEN DROP OWNED BY ident_sin_permiso; DROP ROLE ident_sin_permiso; END IF; END $$"
            )
        )
        c.execute(text("CREATE ROLE ident_sin_permiso LOGIN PASSWORD 'ident_sin_permiso'"))
        base = c.execute(text("SELECT current_database()")).scalar_one()
        c.execute(text(f'GRANT CREATE ON DATABASE "{base}" TO ident_sin_permiso'))
        if sin_usage_en_public:
            c.execute(text("REVOKE USAGE ON SCHEMA public FROM PUBLIC"))

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
            c.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
            c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
            c.execute(text("DROP OWNED BY ident_sin_permiso"))
            c.execute(text("DROP ROLE ident_sin_permiso"))

def test_la_migracion_se_puede_revertir(engine_pg, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", "base")

    assert inspect(engine_pg).get_table_names(schema=SCHEMA) == ["alembic_version"]


def _crear_public_clientes(c):
    """public.clientes tal como la dejó el servicio anterior (create_all), con un cliente."""
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


def test_0005_agrega_ip_y_user_agent_nulos_al_registro_de_consentimiento(base_migrada):
    columnas = {c["name"]: c for c in inspect(base_migrada).get_columns("consentimiento_registros", schema=SCHEMA)}

    assert columnas["ip_origen"]["nullable"] and columnas["user_agent"]["nullable"]
    assert columnas["ip_origen"]["type"].length == 45
    assert columnas["user_agent"]["type"].length == 500


def test_el_registro_con_origen_sigue_siendo_inmutable_en_la_base(base_migrada):
    with base_migrada.begin() as c:
        _insertar_cliente(c, "c1")
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.consentimiento_registros "
                "(id, cliente_id, tipo, evento, version_texto, canal, fecha, ip_origen, user_agent) "
                "VALUES ('r1', 'c1', 'OPEN_FINANCE_OPEN_DATA', 'REVOCADO', 'v1', 'MOVIL', now(), '203.0.113.7', 'App/1')"
            )
        )

    with pytest.raises(DBAPIError, match="inmutable"), base_migrada.begin() as c:
        c.execute(text(f"UPDATE {SCHEMA}.consentimiento_registros SET ip_origen = '1.1.1.1'"))


def test_revertir_la_0005_conserva_los_registros_y_quita_las_columnas(engine_pg, alembic):
    alembic("upgrade", "head")
    with engine_pg.begin() as c:
        _insertar_cliente(c, "c1")
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.consentimiento_registros "
                "(id, cliente_id, tipo, evento, version_texto, canal, fecha, ip_origen) "
                "VALUES ('r1', 'c1', 'OPEN_FINANCE_OPEN_DATA', 'OTORGADO', 'v1', 'MOVIL', now(), '203.0.113.7')"
            )
        )

    alembic("downgrade", "0004")

    nombres = {c["name"] for c in inspect(engine_pg).get_columns("consentimiento_registros", schema=SCHEMA)}
    assert not {"ip_origen", "user_agent"} & nombres
    with engine_pg.connect() as c:
        assert c.execute(text(f"SELECT count(*) FROM {SCHEMA}.consentimiento_registros")).scalar() == 1
    alembic("upgrade", "head")


def test_0006_solo_admite_los_roles_asesor_y_operador(base_migrada):
    with base_migrada.begin() as c:
        for i, rol in enumerate(("ASESOR", "OPERADOR")):
            c.execute(
                text(
                    f"INSERT INTO {SCHEMA}.usuarios_internos (id, email, nombre, password_hash, rol, creado_en) "
                    f"VALUES ('u{i}', 'u{i}@solventa.test', 'U', 'h', '{rol}', now())"
                )
            )
    with pytest.raises(DBAPIError, match="ck_usuarios_internos_rol"), base_migrada.begin() as c:
        c.execute(
            text(
                f"INSERT INTO {SCHEMA}.usuarios_internos (id, email, nombre, password_hash, rol, creado_en) "
                "VALUES ('u9', 'u9@solventa.test', 'U', 'h', 'SUPERUSUARIO', now())"
            )
        )


def test_revertir_la_0006_quita_la_restriccion_de_roles(engine_pg, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", "0005")
    restricciones = {r["name"] for r in inspect(engine_pg).get_check_constraints("usuarios_internos", schema=SCHEMA)}
    assert "ck_usuarios_internos_rol" not in restricciones
    alembic("upgrade", "head")
