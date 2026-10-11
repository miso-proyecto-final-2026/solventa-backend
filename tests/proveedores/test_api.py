from fastapi.testclient import TestClient

from app.main import app
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento


def test_endpoint_devuelve_perfil_crudo_con_origen():
    with TestClient(app) as client:
        r = client.get("/perfiles/c1")
    assert r.status_code == 200
    cuerpo = r.json()
    assert {f["fuente"] for f in cuerpo["fuentes"]} == {"open_finance", "open_data"}
    assert cuerpo["degradado"] is False and cuerpo["por_defecto"] is False


def test_endpoint_sin_consentimiento_devuelve_perfil_por_defecto():
    with TestClient(app) as client:
        client.app.state.servicio_perfil._consentimiento = ConsentimientoStub(
            EstadoConsentimiento.REVOCADO
        )
        r = client.get("/perfiles/c1")
    assert r.status_code == 200
    assert r.json()["por_defecto"] is True and r.json()["fuentes"] == []


def test_openapi_publica_el_endpoint_de_perfiles():
    with TestClient(app) as client:
        assert "/perfiles/{cliente_id}" in client.get("/openapi.json").json()["paths"]
