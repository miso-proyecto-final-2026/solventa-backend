from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_devuelve_ok():
    respuesta = client.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"status": "ok"}


def test_health_registra_la_consulta_manual(caplog):
    with caplog.at_level("INFO", logger="solventa.identidad"):
        client.get("/health")
    assert any(r.campos["evento"] == "health_consultado" for r in caplog.records if hasattr(r, "campos"))


def test_health_no_registra_las_sondas_de_kubernetes(caplog):
    with caplog.at_level("INFO", logger="solventa.identidad"):
        client.get("/health", headers={"User-Agent": "kube-probe/1.35"})
    assert not [r for r in caplog.records if r.name == "solventa.identidad"]
