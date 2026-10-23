import uuid

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.eventos.registro import RegistroEventos, tabla_eventos_procesados


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
