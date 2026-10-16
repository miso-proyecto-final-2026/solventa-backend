import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import db
from app.db import crear_engine
from app.idempotency import _cache
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
    _cache.clear()
    yield
    app.dependency_overrides.clear()
    _cache.clear()
    engine.dispose()


@pytest.fixture
def client():
    return TestClient(app)
