from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.cotizacion.modelos_db import SCHEMA
from app.cotizacion.reglas import cargar_reglas


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
