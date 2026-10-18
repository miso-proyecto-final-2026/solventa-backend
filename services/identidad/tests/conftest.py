import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import db
from app.db import crear_engine
from app.main import app


@pytest.fixture(autouse=True)
def _base_de_datos_limpia():
    engine = crear_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(bind=engine)
    SessionDePrueba = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _get_db_de_prueba():
        sesion = SessionDePrueba()
        try:
            yield sesion
        finally:
            sesion.close()

    from app.routers.clientes import get_db as get_db_dependencia

    app.dependency_overrides[get_db_dependencia] = _get_db_de_prueba
    yield SessionDePrueba
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sesion_bd(_base_de_datos_limpia):
    """Sesión sobre la misma base en memoria que usa la API, para inspeccionar lo que se persistió."""
    sesion = _base_de_datos_limpia()
    yield sesion
    sesion.close()
