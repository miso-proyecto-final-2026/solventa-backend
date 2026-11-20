"""`POST /oauth/token` (HU32, criterio 2): client credentials, token de 1 hora y errores de RFC 6749."""

import base64
import logging

import jwt
from app.socios.modelos_db import ESTADO_INACTIVO, ClienteApiDB
from sqlalchemy import update

from tests.dobles import SECRETO_JWT_PRUEBA, SECRETO_PRUEBA


def _basic(usuario: str, clave: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{usuario}:{clave}".encode()).decode()}


def _pedir(cliente, **datos):
    return cliente.post("/oauth/token", data={"grant_type": "client_credentials", **datos})


def test_emite_un_token_de_una_hora_con_credenciales_en_el_cuerpo(cliente, sembrar):
    sembrar()

    r = _pedir(cliente, client_id="socio-a", client_secret=SECRETO_PRUEBA)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["token_type"] == "Bearer"
    assert cuerpo["expires_in"] == 3600
    assert cuerpo["scope"] == "cotizaciones:crear"
    assert r.headers["Cache-Control"] == "no-store"
    claims = jwt.decode(cuerpo["access_token"], SECRETO_JWT_PRUEBA, algorithms=["HS256"], audience="solventa-partners")
    assert claims["sub"] == "socio-a"
    assert claims["iss"] == "solventa-socios"
    assert claims["exp"] - claims["iat"] == 3600


def test_acepta_credenciales_por_http_basic(cliente, sembrar):
    sembrar()

    r = cliente.post("/oauth/token", data={"grant_type": "client_credentials"}, headers=_basic("socio-a", SECRETO_PRUEBA))

    assert r.status_code == 200


def test_rechaza_un_secreto_incorrecto_con_invalid_client(cliente, sembrar):
    sembrar()

    r = _pedir(cliente, client_id="socio-a", client_secret="otro-secreto-que-no-es-el-del-socio")

    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
    assert r.json()["trace_id"]


def test_rechaza_un_socio_inexistente_igual_que_un_secreto_incorrecto(cliente):
    r = _pedir(cliente, client_id="no-existe", client_secret=SECRETO_PRUEBA)

    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_basic_incorrecto_responde_www_authenticate(cliente, sembrar):
    sembrar()

    r = cliente.post("/oauth/token", data={"grant_type": "client_credentials"}, headers=_basic("socio-a", "malo"))

    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")


def test_basic_mal_codificado_es_una_solicitud_invalida(cliente):
    r = cliente.post(
        "/oauth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": "Basic %%%no-base64"}
    )

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_rechaza_un_socio_inactivo(cliente, sembrar, fabrica):
    sembrar()
    with fabrica.begin() as sesion:
        sesion.execute(update(ClienteApiDB).values(estado=ESTADO_INACTIVO))

    r = _pedir(cliente, client_id="socio-a", client_secret=SECRETO_PRUEBA)

    assert r.status_code == 401


def test_socio_sin_contrato_recibe_token_sin_alcance(cliente, sembrar):
    sembrar(version="v0")

    r = _pedir(cliente, client_id="socio-a", client_secret=SECRETO_PRUEBA)

    assert r.status_code == 200
    assert r.json()["scope"] == ""


def test_falta_grant_type(cliente):
    r = cliente.post("/oauth/token", data={"client_id": "a", "client_secret": "b"})

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_grant_no_soportado(cliente):
    r = cliente.post("/oauth/token", data={"grant_type": "password", "client_id": "a", "client_secret": "b"})

    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_faltan_credenciales(cliente):
    r = _pedir(cliente, client_id="socio-a")

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_no_acepta_credenciales_por_dos_vias(cliente, sembrar):
    sembrar()

    r = cliente.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "socio-a", "client_secret": SECRETO_PRUEBA},
        headers=_basic("socio-a", SECRETO_PRUEBA),
    )

    assert r.status_code == 400


def test_ni_el_secreto_ni_el_token_quedan_en_los_logs(cliente, sembrar, caplog):
    sembrar()
    caplog.set_level(logging.DEBUG)

    ok = _pedir(cliente, client_id="socio-a", client_secret=SECRETO_PRUEBA)
    _pedir(cliente, client_id="socio-a", client_secret="secreto-equivocado-para-el-log")

    texto = "\n".join(r.getMessage() + str(getattr(r, "campos", "")) for r in caplog.records)
    assert "token_emitido" in texto
    assert SECRETO_PRUEBA not in texto
    assert "secreto-equivocado-para-el-log" not in texto
    assert ok.json()["access_token"] not in texto
