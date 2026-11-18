from datetime import UTC, datetime

import fakeredis
import pytest
from app.socios.almacen import AlmacenRedis
from app.socios.cuota import LimitadorCuota
from app.socios.idempotencia import Idempotencia
from app.socios.registro import RegistroSocios
from app.socios.repositorio import crear_engine, crear_tablas_locales, guardar_socio
from app.socios.secretos import hash_secreto
from app.socios.tokens import EmisorTokens
from app.socios.wiring import Ensamblado, publicar
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from tests.dobles import SECRETO_JWT_PRUEBA, SECRETO_PRUEBA, CotizacionesFalsas

# bcrypt con el costo mínimo: las pruebas no miden el costo, solo el comportamiento.
RONDAS_PRUEBA = 4


@pytest.fixture(autouse=True)
def modo_desarrollo_por_defecto(monkeypatch):
    monkeypatch.setenv("SOLVENTA_MODO_DESARROLLO", "1")


@pytest.fixture
def fabrica():
    """Base de datos SQLite en memoria con las tablas del servicio (en PostgreSQL las crea Alembic)."""
    engine = crear_engine("sqlite://")
    crear_tablas_locales(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def sembrar(fabrica):
    def _sembrar(client_id="socio-a", endpoints=("cotizaciones:crear",), cuota_rps=50, secreto=SECRETO_PRUEBA, version="v1"):
        with fabrica.begin() as sesion:
            return guardar_socio(
                sesion,
                client_id=client_id,
                nombre_socio=f"Socio {client_id}",
                client_secret_hash=hash_secreto(secreto, RONDAS_PRUEBA),
                version=version,
                endpoints=endpoints,
                cuota_rps=cuota_rps,
                ahora=datetime(2026, 1, 1, tzinfo=UTC),
            )

    return _sembrar


@pytest.fixture
def almacen():
    # Un servidor propio por prueba: los contadores de cuota y las claves no se filtran entre pruebas.
    return AlmacenRedis(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True))


@pytest.fixture
def cotizaciones():
    return CotizacionesFalsas()


@pytest.fixture
def ensamblado(fabrica, almacen, cotizaciones):
    return Ensamblado(
        # Sin caché de contratos en las pruebas de la API: cada cambio de contrato se ve de inmediato.
        registro=RegistroSocios(fabrica, ttl_contratos_s=0),
        tokens=EmisorTokens(SECRETO_JWT_PRUEBA, 3600),
        almacen=almacen,
        # Reloj fijo: todas las solicitudes de una prueba caen en el mismo segundo de la ventana de cuota.
        limitador=LimitadorCuota(almacen, reloj=lambda: 1_800_000_000.0),
        idempotencia=Idempotencia(almacen),
        cotizaciones=cotizaciones,
    )


@pytest.fixture
def cliente(ensamblado):
    """TestClient sin lifespan: el ensamblado de prueba reemplaza al del entorno."""
    publicar(app.state, ensamblado)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def token(cliente, sembrar):
    def _token(client_id="socio-a", **opciones):
        sembrar(client_id=client_id, **opciones)
        r = cliente.post(
            "/oauth/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": SECRETO_PRUEBA},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    return _token


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


@pytest.fixture(scope="session")
def redis_url():
    if not _docker_disponible():
        pytest.skip("Docker no disponible: se omiten las pruebas con contenedores")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"
