import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento

CLAVE = "clave-de-prueba"
CABECERAS = {"X-Service-Key": CLAVE}


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("PERFILES_API_KEYS", f"otra-clave, {CLAVE}")
    with TestClient(app) as client:
        yield client


def test_endpoint_devuelve_perfil_crudo_con_origen(cliente):
    r = cliente.get("/perfiles/c1", headers=CABECERAS)
    assert r.status_code == 200
    cuerpo = r.json()
    assert {f["fuente"] for f in cuerpo["fuentes"]} == {"open_finance", "open_data"}
    assert cuerpo["degradado"] is False and cuerpo["por_defecto"] is False


def test_endpoint_sin_consentimiento_devuelve_perfil_por_defecto(cliente):
    cliente.app.state.servicio_perfil._consentimiento = ConsentimientoStub(
        EstadoConsentimiento.REVOCADO
    )
    r = cliente.get("/perfiles/c1", headers=CABECERAS)
    assert r.status_code == 200
    assert r.json()["por_defecto"] is True and r.json()["fuentes"] == []


def test_sin_clave_responde_401_y_no_consulta_proveedores(cliente):
    r = cliente.get("/perfiles/c1")
    assert r.status_code == 401
    assert "fuentes" not in r.text


def test_clave_incorrecta_responde_403(cliente):
    r = cliente.get("/perfiles/c1", headers={"X-Service-Key": "incorrecta"})
    assert r.status_code == 403


def test_sin_claves_configuradas_nadie_accede(monkeypatch):
    monkeypatch.delenv("PERFILES_API_KEYS", raising=False)
    with TestClient(app) as client:
        r = client.get("/perfiles/c1", headers=CABECERAS)
    assert r.status_code == 403


def test_la_clave_de_auditoria_no_abre_el_perfil(monkeypatch):
    """Cada rol tiene su propia clave: la del auditor de HU14 no sirve aquí."""
    monkeypatch.setenv("AUDITOR_API_KEYS", "clave-auditor")
    monkeypatch.setenv("PERFILES_API_KEYS", CLAVE)
    with TestClient(app) as client:
        r = client.get("/perfiles/c1", headers={"X-Service-Key": "clave-auditor"})
    assert r.status_code == 403


def test_openapi_publica_el_endpoint_de_perfiles(cliente):
    paths = cliente.get("/openapi.json").json()["paths"]
    assert "/perfiles/{cliente_id}" in paths
