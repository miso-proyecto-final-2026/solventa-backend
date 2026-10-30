"""HU20 — `GET /v1/resumenes-cliente/{id}`: un servicio interno pide los datos del cliente para la póliza."""

import pytest

from app.routers.resumenes_cliente import enmascarar_documento
from tests.helpers import PAYLOAD_CLIENTE, registrar

CLAVE_SERVICIO = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", f"{CLAVE_SERVICIO},otra-clave")


def _consultar(client, cliente_id, clave=CLAVE_SERVICIO):
    cabeceras = {"X-Service-Key": clave} if clave is not None else {}
    return client.get(f"/v1/resumenes-cliente/{cliente_id}", headers=cabeceras)


def test_con_clave_de_servicio_devuelve_nombre_completo_y_documento_enmascarado(client):
    cliente_id, _ = registrar(client)

    r = _consultar(client, cliente_id)

    assert r.status_code == 200
    assert r.json() == {
        "cliente_id": cliente_id,
        "nombre": "Ana",
        "apellido": "Pérez",
        "tipo_documento": "CC",
        "documento_enmascarado": "******300",
    }


def test_no_expone_el_documento_completo_ni_el_correo_ni_el_celular(client):
    cliente_id, _ = registrar(client)

    texto = _consultar(client, cliente_id).text

    assert PAYLOAD_CLIENTE["numero_documento"] not in texto
    assert PAYLOAD_CLIENTE["email"] not in texto
    assert PAYLOAD_CLIENTE["celular"] not in texto


def test_sin_clave_o_con_clave_invalida_es_401(client):
    cliente_id, _ = registrar(client)

    assert _consultar(client, cliente_id, clave=None).status_code == 401
    assert _consultar(client, cliente_id, clave="clave-incorrecta").status_code == 401


def test_un_token_de_cliente_no_reemplaza_la_clave_de_servicio(client):
    cliente_id, auth = registrar(client)

    r = client.get(f"/v1/resumenes-cliente/{cliente_id}", headers=auth)

    assert r.status_code == 401


def test_un_cliente_inexistente_es_404(client):
    assert _consultar(client, "no-existe").status_code == 404


@pytest.mark.parametrize(
    ("numero", "esperado"),
    [("100200300", "******300"), ("12345", "**345"), ("123", "***"), ("1", "*")],
)
def test_enmascarar_documento(numero, esperado):
    assert enmascarar_documento(numero) == esperado
