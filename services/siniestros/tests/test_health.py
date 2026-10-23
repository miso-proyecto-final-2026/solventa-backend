from fastapi.testclient import TestClient

from app.main import app


def test_health_devuelve_ok():
    with TestClient(app) as cliente:
        respuesta = cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"status": "ok"}
