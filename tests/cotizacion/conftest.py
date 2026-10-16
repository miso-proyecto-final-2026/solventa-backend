from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.cotizacion.api import router
from app.cotizacion.dominio import PerfilUnificado
from app.cotizacion.errores import registrar_errores
from app.cotizacion.modelos_db import SCHEMA
from app.cotizacion.reglas import cargar_reglas
from app.cotizacion.repositorio import (
    RepositorioCotizaciones,
    crear_engine,
    crear_tablas_locales,
)
from app.cotizacion.servicio import ServicioCotizacion


@pytest.fixture(scope="session")
def reglas():
    return cargar_reglas("v1")


RAIZ = Path(__file__).parents[2]


def _docker_disponible() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001  (cualquier fallo de Docker = omitir la prueba)
        return False


@pytest.fixture(scope="session")
def postgres_url():
    if not _docker_disponible():
        pytest.skip("Docker no disponible: se omiten las pruebas con contenedores")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture
def alembic(postgres_url, monkeypatch):
    """Ejecuta comandos de Alembic (upgrade/downgrade) contra el Postgres del contenedor."""

    def _ejecutar(accion: str, destino: str) -> None:
        monkeypatch.setenv("COTIZACION_DATABASE_URL", postgres_url)
        cfg = Config(str(RAIZ / "alembic.ini"))
        cfg.set_main_option("script_location", str(RAIZ / "migrations"))
        getattr(command, accion)(cfg, destino)

    return _ejecutar


@pytest.fixture
def engine_pg(postgres_url):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    yield engine
    engine.dispose()


@pytest.fixture
def base_migrada(engine_pg, alembic):
    alembic("upgrade", "head")
    return engine_pg


PERFIL_CON_SENALES = PerfilUnificado(
    "x",
    senales={
        "ratio_endeudamiento": 0.32,
        "reportes_negativos": 0,
        "ingresos_mensuales_cop": 4_500_000,
    },
    hit_cache=True,
    fuentes=("open_finance", "open_data"),
)


class PerfilFijo:
    """Perfil de prueba que cuenta cuántas veces se consultó."""

    def __init__(self, perfil: PerfilUnificado) -> None:
        self.perfil = perfil
        self.llamadas = 0

    async def obtener(self, cliente_id: str) -> PerfilUnificado:
        self.llamadas += 1
        return replace(self.perfil, cliente_id=cliente_id)


class Entorno:
    def __init__(self, client, engine, servicio, perfiles, repositorio):
        self.client = client
        self.engine = engine
        self.servicio = servicio
        self.perfiles = perfiles
        self.repositorio = repositorio

    def filas(self, tabla: str) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(text(f"SELECT * FROM {tabla}"))]


@pytest.fixture
def entorno(reglas):
    """Fábrica de app de cotización con SQLite en memoria y perfil configurable."""
    creados = []

    def _crear(
        perfil: PerfilUnificado = PERFIL_CON_SENALES, **servicio_kwargs
    ) -> Entorno:
        engine = crear_engine("sqlite:///:memory:")
        crear_tablas_locales(engine)
        repo = RepositorioCotizaciones(sessionmaker(engine, expire_on_commit=False))
        perfiles = PerfilFijo(perfil)
        servicio = ServicioCotizacion(reglas, perfiles, repo, **servicio_kwargs)
        app = FastAPI()
        app.include_router(router)
        registrar_errores(app)
        app.state.servicio_cotizacion = servicio
        creados.append(engine)
        return Entorno(TestClient(app), engine, servicio, perfiles, repo)

    yield _crear
    for e in creados:
        e.dispose()


@pytest.fixture(scope="session")
def redis_url():
    if not _docker_disponible():
        pytest.skip("Docker no disponible: se omiten las pruebas con contenedores")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"
