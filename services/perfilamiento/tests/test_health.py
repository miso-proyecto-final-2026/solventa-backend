from fastapi.testclient import TestClient

from app.main import app


def test_health_devuelve_ok():
    respuesta = TestClient(app).get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"status": "ok"}
