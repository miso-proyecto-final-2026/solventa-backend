import socket

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


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def kafka_brokers():
    """Un Kafka real (KRaft, un nodo) para toda la sesión de pruebas."""
    if not _docker_disponible():
        pytest.skip("Docker no disponible: se omiten las pruebas con Kafka real")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    puerto = _puerto_libre()
    contenedor = (
        DockerContainer("apache/kafka:3.9.0")
        .with_bind_ports(9092, puerto)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", "PLAINTEXT://:9092,CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://localhost:{puerto}")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT")
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
        .waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(120))
    )
    with contenedor:
        yield [f"localhost:{puerto}"]
