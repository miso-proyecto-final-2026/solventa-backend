"""HU33 — `POST /v1/interno/clientes/identificacion`: MS Socios identifica al cliente de la venta embebida."""

import pytest

from app.models import Cliente
from tests.helpers import PAYLOAD_CLIENTE, registrar

CLAVE_SERVICIO = "clave-de-prueba"
RUTA = "/v1/interno/clientes/identificacion"
DOCUMENTO = {"tipo_documento": "CC", "numero_documento": PAYLOAD_CLIENTE["numero_documento"]}


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", f"{CLAVE_SERVICIO},otra-clave")


def _identificar(client, cuerpo=None, clave=CLAVE_SERVICIO):
    cabeceras = {"X-Service-Key": clave} if clave is not None else {}
    return client.post(RUTA, json=cuerpo if cuerpo is not None else DOCUMENTO, headers=cabeceras)


def _otorgar(client, cliente_id, auth):
    r = client.post(
        f"/v1/clientes/{cliente_id}/consentimientos",
        json={"tipo": "OPEN_FINANCE_OPEN_DATA", "version_texto": "v1", "canal": "WEB"},
        headers=auth,
    )
    assert r.status_code == 201


def test_cliente_con_consentimiento_devuelve_id_kyc_y_vigencia(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    sesion_bd.query(Cliente).filter_by(id=cliente_id).update({"estado_kyc": "VERIFICADO"})
    sesion_bd.commit()

    r = _identificar(client)

    assert r.status_code == 200
    assert r.json() == {"cliente_id": cliente_id, "estado_kyc": "VERIFICADO", "consentimiento_vigente": True}


def test_sin_consentimiento_la_vigencia_es_falsa_y_el_kyc_pendiente(client):
    cliente_id, _ = registrar(client)

    r = _identificar(client)

    assert r.json() == {"cliente_id": cliente_id, "estado_kyc": "PENDIENTE", "consentimiento_vigente": False}


def test_con_consentimiento_revocado_no_esta_vigente(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    revocado = client.delete(f"/v1/clientes/{cliente_id}/consentimientos/OPEN_FINANCE_OPEN_DATA", headers=auth)
    assert revocado.status_code == 200

    assert _identificar(client).json()["consentimiento_vigente"] is False


def test_no_expone_datos_personales(client):
    registrar(client)

    texto = _identificar(client).text

    for dato in ("Ana", "Pérez", PAYLOAD_CLIENTE["email"], PAYLOAD_CLIENTE["celular"], PAYLOAD_CLIENTE["numero_documento"]):
        assert dato not in texto


def test_sin_clave_o_con_clave_invalida_es_401(client):
    registrar(client)

    assert _identificar(client, clave=None).status_code == 401
    assert _identificar(client, clave="incorrecta").status_code == 401


def test_un_token_de_cliente_no_reemplaza_la_clave_de_servicio(client):
    _, auth = registrar(client)

    assert client.post(RUTA, json=DOCUMENTO, headers=auth).status_code == 401


def test_cliente_inexistente_es_404(client):
    assert _identificar(client, {"tipo_documento": "CC", "numero_documento": "99999999"}).status_code == 404


@pytest.mark.parametrize("tipo,numero", [("XX", "12345678"), ("CC", "12")])
def test_documento_invalido_es_422(client, tipo, numero):
    assert _identificar(client, {"tipo_documento": tipo, "numero_documento": numero}).status_code == 422
