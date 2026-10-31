import pytest
from app.notificaciones.canales import PushSimulado
from app.notificaciones.repositorio import crear_engine, crear_tablas_locales
from app.notificaciones.servicio import ServicioNotificaciones
from sqlalchemy.orm import sessionmaker

from tests.dobles import CertificadosFalsos, CorreoFalso, IdentidadFalsa, Reloj


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
    """Fábrica de servicios con vecinos y canales falsos; cada parámetro reemplaza a su doble. El push es el simulado, de verdad."""

    def _armar(**opciones):
        opciones.setdefault("identidad", IdentidadFalsa())
        opciones.setdefault("certificados", CertificadosFalsos())
        opciones.setdefault("correo", CorreoFalso())
        opciones.setdefault("push", PushSimulado(fabrica, reloj=reloj))
        opciones.setdefault("url_publica", "https://borde.example")
        opciones.setdefault("espera_base_s", 5)
        return ServicioNotificaciones(fabrica=fabrica, reloj=reloj, **opciones)

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
