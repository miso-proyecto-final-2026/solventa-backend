import pytest
from app.pagos.repositorio import crear_engine, crear_tablas_locales
from app.pagos.servicio import ServicioCobros
from sqlalchemy.orm import sessionmaker

from tests.dobles import PasarelaFalsa, Reloj


@pytest.fixture
def fabrica():
    """Base de datos SQLite en memoria con las tablas del servicio (en PostgreSQL las crea Alembic)."""
    engine = crear_engine("sqlite://")
    crear_tablas_locales(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def armar(fabrica, reloj):
    """Fábrica de servicios con una pasarela falsa; `pasarela` la reemplaza."""

    def _armar(pasarela=None, **opciones):
        return ServicioCobros(
            fabrica=fabrica,
            pasarela=pasarela if pasarela is not None else PasarelaFalsa(),
            periodo_gracia_dias=opciones.pop("periodo_gracia_dias", 3),
            timeout_pasarela_s=opciones.pop("timeout_pasarela_s", 0.2),
            reintento_base_s=opciones.pop("reintento_base_s", 5),
            reloj=reloj,
            **opciones,
        )

    return _armar


@pytest.fixture(autouse=True)
def modo_desarrollo_por_defecto(monkeypatch):
    monkeypatch.setenv("SOLVENTA_MODO_DESARROLLO", "1")


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
