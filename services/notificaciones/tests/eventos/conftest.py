import socket
import uuid

import pytest
from app.eventos.registro import RegistroEventos, tabla_eventos_procesados
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _docker_disponible() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001  (cualquier fallo de Docker = omitir la prueba)
        return False


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


@pytest.fixture
def topic():
    return f"prueba.{uuid.uuid4().hex[:10]}"


@pytest.fixture
def base():
    """Base de datos en memoria con la tabla de idempotencia y una tabla de efectos para probar el rollback."""
    metadata = MetaData()
    tabla = tabla_eventos_procesados(metadata)
    efectos = Table("efectos", metadata, Column("id", Integer, primary_key=True, autoincrement=True), Column("valor", String(50)))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    # Receta de SQLAlchemy para que SQLite respete transacciones y savepoints como PostgreSQL (el controlador de Python no lo hace).
    @event.listens_for(engine, "connect")
    def _sin_autotransaccion(conexion_dbapi, _registro):
        conexion_dbapi.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_explicito(conexion):
        conexion.exec_driver_sql("BEGIN")

    metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    return type("Base", (), {"fabrica": fabrica, "registro": RegistroEventos(tabla), "tabla": tabla, "efectos": efectos})
