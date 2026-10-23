"""HU15 — `GET /v1/sesion/actual`: un servicio interno pregunta de quién es el token de un cliente."""

import pytest

from tests.helpers import registrar, registrar_segundo

CLAVE_SERVICIO = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", f"{CLAVE_SERVICIO},otra-clave")


def _consultar(client, token=None, clave=CLAVE_SERVICIO):
    cabeceras = {}
    if token is not None:
        cabeceras["Authorization"] = f"Bearer {token}"
    if clave is not None:
        cabeceras["X-Service-Key"] = clave
    return client.get("/v1/sesion/actual", headers=cabeceras)


def _token(auth):
    return auth["Authorization"].removeprefix("Bearer ")


def test_con_clave_de_servicio_y_token_valido_devuelve_el_cliente(client):
    cliente_id, auth = registrar(client)

    r = _consultar(client, _token(auth))

    assert r.status_code == 200
    assert r.json() == {"cliente_id": cliente_id}


def test_cada_token_devuelve_a_su_dueno(client):
    primero, auth1 = registrar(client)
    segundo, auth2 = registrar_segundo(client)

    assert _consultar(client, _token(auth1)).json()["cliente_id"] == primero
    assert _consultar(client, _token(auth2)).json()["cliente_id"] == segundo


def test_un_token_desconocido_es_401(client):
    registrar(client)

    assert _consultar(client, "token-que-no-existe").status_code == 401


def test_sin_token_es_401(client):
    assert _consultar(client, token=None).status_code == 401


def test_sin_clave_de_servicio_es_401_aunque_el_token_sea_valido(client):
    _, auth = registrar(client)

    assert _consultar(client, _token(auth), clave=None).status_code == 401


def test_con_una_clave_de_servicio_invalida_es_401(client):
    _, auth = registrar(client)

    assert _consultar(client, _token(auth), clave="clave-ajena").status_code == 401


def test_sin_claves_de_servicio_configuradas_nadie_accede(client, monkeypatch):
    _, auth = registrar(client)
    monkeypatch.delenv("SERVICE_API_KEYS", raising=False)

    assert _consultar(client, _token(auth)).status_code == 401


def test_la_respuesta_no_trae_datos_personales(client):
    _, auth = registrar(client)

    cuerpo = _consultar(client, _token(auth)).json()

    assert set(cuerpo) == {"cliente_id"}


def test_el_token_de_un_cliente_solo_no_basta_aunque_pase_por_el_endpoint_de_clientes(client):
    """Quien tiene su token no puede usar este endpoint interno para nada: exige la clave de servicio."""
    cliente_id, auth = registrar(client)

    assert client.get("/v1/sesion/actual", headers=auth).status_code == 401
    assert client.get(f"/v1/clientes/{cliente_id}/consentimientos/OPEN_FINANCE_OPEN_DATA", headers=auth).status_code == 200
