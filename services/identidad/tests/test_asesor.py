"""Login interno del asesor y búsqueda de cliente por documento (HU38)."""

import time

import jwt
import pytest

from app.auth import obtener_emisor
from app.main import app
from app.models import UsuarioInterno
from app.security import hash_password
from app.tokens import EmisorJwtHs256

from .helpers import PAYLOAD_CLIENTE, registrar

SECRETO = "secreto-de-prueba-con-32-bytes-o-mas!"
CLAVE = "Asesor1234"


@pytest.fixture
def emisor():
    e = EmisorJwtHs256(SECRETO, ttl_min=30)
    app.dependency_overrides[obtener_emisor] = lambda: e
    return e


def _crear_usuario(sesion_bd, email="asesor@solventa.test", rol="ASESOR", activo=True):
    u = UsuarioInterno(
        email=email, nombre="Asesora Demo", password_hash=hash_password(CLAVE), rol=rol, activo=activo
    )
    sesion_bd.add(u)
    sesion_bd.commit()
    return u


def _cabeceras(client, **kw):
    r = client.post("/v1/sesiones-internas", json={"email": "asesor@solventa.test", "password": CLAVE, **kw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_correcto_devuelve_token_con_rol_asesor(client, sesion_bd, emisor):
    _crear_usuario(sesion_bd)
    r = client.post("/v1/sesiones-internas", json={"email": "ASESOR@solventa.test", "password": CLAVE})
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["token_type"] == "bearer"
    assert cuerpo["expira_en"] == 1800
    assert cuerpo["usuario"]["rol"] == "ASESOR"
    claims = emisor.verificar(cuerpo["access_token"])
    assert claims is not None and claims.rol == "ASESOR"


@pytest.mark.parametrize("email,password", [("asesor@solventa.test", "otra"), ("nadie@solventa.test", CLAVE)])
def test_login_incorrecto_responde_401_con_el_mismo_mensaje(client, sesion_bd, emisor, email, password):
    _crear_usuario(sesion_bd)
    r = client.post("/v1/sesiones-internas", json={"email": email, "password": password})
    assert r.status_code == 401
    assert r.json()["detail"] == "Correo o contraseña incorrectos"


def test_login_de_usuario_inactivo_responde_401(client, sesion_bd, emisor):
    _crear_usuario(sesion_bd, activo=False)
    r = client.post("/v1/sesiones-internas", json={"email": "asesor@solventa.test", "password": CLAVE})
    assert r.status_code == 401


def test_busqueda_encontrada_devuelve_datos_minimos_y_consentimiento(client, sesion_bd, emisor):
    _crear_usuario(sesion_bd)
    cliente_id, sesion_cliente = registrar(client)
    client.post(
        f"/v1/clientes/{cliente_id}/consentimientos",
        json={"tipo": "OPEN_FINANCE_OPEN_DATA", "version_texto": "v1", "canal": "WEB"},
        headers=sesion_cliente,
    )
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": "CC", "numero_documento": PAYLOAD_CLIENTE["numero_documento"]},
        headers=_cabeceras(client),
    )
    assert r.status_code == 200
    c = r.json()
    assert c["cliente_id"] == cliente_id
    assert c["apellido_inicial"] == "P."
    assert c["consentimiento"]["estado"] == "OTORGADO"
    assert "email" not in c and "celular" not in c


def test_busqueda_sin_consentimiento_informa_no_otorgado(client, sesion_bd, emisor):
    _crear_usuario(sesion_bd)
    registrar(client)
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": "CC", "numero_documento": PAYLOAD_CLIENTE["numero_documento"]},
        headers=_cabeceras(client),
    )
    assert r.json()["consentimiento"]["estado"] == "NO_OTORGADO"


def test_busqueda_de_documento_inexistente_responde_404(client, sesion_bd, emisor):
    _crear_usuario(sesion_bd)
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": "CC", "numero_documento": "99999999"},
        headers=_cabeceras(client),
    )
    assert r.status_code == 404


@pytest.mark.parametrize("tipo,numero", [("XX", "12345678"), ("CC", "12")])
def test_busqueda_con_documento_invalido_responde_422(client, sesion_bd, emisor, tipo, numero):
    _crear_usuario(sesion_bd)
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": tipo, "numero_documento": numero},
        headers=_cabeceras(client),
    )
    assert r.status_code == 422


def test_busqueda_sin_token_responde_401(client, emisor):
    r = client.get("/v1/clientes/busqueda", params={"tipo_documento": "CC", "numero_documento": "12345678"})
    assert r.status_code == 401


def test_busqueda_con_token_de_cliente_responde_401(client, emisor):
    _, sesion_cliente = registrar(client)
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": "CC", "numero_documento": "12345678"},
        headers=sesion_cliente,
    )
    assert r.status_code == 401


def test_busqueda_con_token_de_otro_rol_responde_403(client, emisor):
    token = emisor.emitir("u-1", "AUDITOR", "Otro")
    r = client.get(
        "/v1/clientes/busqueda",
        params={"tipo_documento": "CC", "numero_documento": "12345678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_token_vencido_o_mal_firmado_responde_401(client, emisor):
    ahora = int(time.time())
    base = {"sub": "u", "rol": "ASESOR", "iss": "solventa-identidad", "iat": ahora}
    vencido = jwt.encode({**base, "exp": ahora - 10}, SECRETO, algorithm="HS256")
    ajeno = jwt.encode({**base, "exp": ahora + 600}, "otro-secreto-con-32-bytes-o-mas-xx", algorithm="HS256")
    for token in (vencido, ajeno):
        r = client.get(
            "/v1/clientes/busqueda",
            params={"tipo_documento": "CC", "numero_documento": "12345678"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401
