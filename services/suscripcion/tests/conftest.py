import pytest
from app.suscripcion.reglas import cargar_reglas
from app.suscripcion.repositorio import crear_engine, crear_tablas_locales
from app.suscripcion.servicio import ServicioSuscripcion
from sqlalchemy.orm import sessionmaker

from tests.dobles import AHORA, CotizacionesFalsas, IdentidadFalsa, PerfilesFalsos


@pytest.fixture
def fabrica():
    """Base de datos SQLite en memoria con las tablas del servicio (en PostgreSQL las crea Alembic)."""
    engine = crear_engine("sqlite://")
    crear_tablas_locales(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def armar(fabrica):
    """Fábrica de servicios con dobles; los parámetros reemplazan a los dobles por defecto."""

    def _armar(identidad=None, cotizaciones=None, perfiles=None):
        return ServicioSuscripcion(
            fabrica=fabrica,
            identidad=identidad or IdentidadFalsa(),
            cotizaciones=cotizaciones or CotizacionesFalsas(),
            perfiles=perfiles or PerfilesFalsos(),
            reglas=cargar_reglas(),
            reloj=lambda: AHORA,
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
