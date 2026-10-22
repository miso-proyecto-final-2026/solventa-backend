import pytest

from app.configuracion import VARIABLE_MODO_DESARROLLO


@pytest.fixture(autouse=True)
def modo_desarrollo_por_defecto(monkeypatch):
    """La suite arranca la app sin Identidad ni Redis reales; las pruebas de
    `test_configuracion.py` quitan el modo para verificar que sin él no arranca."""
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")


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
