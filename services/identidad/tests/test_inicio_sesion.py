"""HU05 — `POST /v1/clientes/sesion`: inicio de sesión de clientes con límite de intentos."""

from datetime import UTC, datetime, timedelta

import pytest

from app import acceso
from app.models import Cliente, IntentoAcceso
from tests.helpers import PAYLOAD_CLIENTE, registrar

EMAIL = PAYLOAD_CLIENTE["email"]
CONTRASENA = PAYLOAD_CLIENTE["password"]


@pytest.fixture
def reloj(monkeypatch):
    """Reloj controlable del límite: avanza con `reloj.avanzar(...)`."""

    class Reloj:
        actual = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

        def avanzar(self, **kw):
            self.actual += timedelta(**kw)

    r = Reloj()
    monkeypatch.setattr(acceso, "ahora", lambda: r.actual)
    return r


def _entrar(client, email=EMAIL, password=CONTRASENA):
    return client.post("/v1/clientes/sesion", json={"email": email, "password": password})


def test_con_credenciales_correctas_devuelve_una_sesion_nueva_que_funciona(client):
    cliente_id, auth_registro = registrar(client)

    r = _entrar(client)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["id"] == cliente_id and cuerpo["email"] == EMAIL and cuerpo["nombre"] == "Ana"
    assert cuerpo["estado_kyc"] == "PENDIENTE"
    nueva = {"Authorization": f"Bearer {cuerpo['access_token']}"}
    assert nueva != auth_registro
    assert client.get(f"/v1/clientes/{cliente_id}/biometria", headers=nueva).status_code == 200


def test_el_correo_no_distingue_mayusculas_ni_espacios(client):
    registrar(client)

    assert _entrar(client, email="  ANA.Perez@Example.com ").status_code == 200


def test_contrasena_incorrecta_es_401_sin_pistas(client):
    registrar(client)

    r = _entrar(client, password="Otra12345")

    assert r.status_code == 401
    assert r.json()["detail"]["codigo"] == "CREDENCIALES_INVALIDAS"


def test_un_correo_que_no_existe_responde_igual_que_una_contrasena_incorrecta(client):
    registrar(client)

    inexistente = _entrar(client, email="nadie@example.com")
    incorrecta = _entrar(client, password="Otra12345")

    assert inexistente.status_code == incorrecta.status_code == 401
    assert inexistente.json() == incorrecta.json()


def test_el_token_no_se_guarda_en_claro_ni_se_reutiliza(client, sesion_bd):
    registrar(client)

    token = _entrar(client).json()["access_token"]
    otro = _entrar(client).json()["access_token"]

    assert token != otro


def test_cinco_fallos_bloquean_y_el_siguiente_intento_ya_no_se_evalua(client, reloj):
    registrar(client)

    for _ in range(4):
        assert _entrar(client, password="mala").status_code == 401
    quinto = _entrar(client, password="mala")

    assert quinto.status_code == 429
    assert quinto.json()["detail"]["codigo"] == "BLOQUEADO"
    assert int(quinto.headers["Retry-After"]) == 15 * 60
    # Ni siquiera la contraseña correcta entra mientras dura el bloqueo.
    assert _entrar(client).status_code == 429


def test_el_bloqueo_se_levanta_a_los_15_minutos(client, reloj):
    registrar(client)
    for _ in range(5):
        _entrar(client, password="mala")

    reloj.avanzar(minutes=14, seconds=59)
    assert _entrar(client).status_code == 429
    reloj.avanzar(seconds=2)

    assert _entrar(client).status_code == 200


def test_un_acceso_correcto_reinicia_la_cuenta_de_fallos(client, reloj, sesion_bd):
    registrar(client)
    for _ in range(4):
        _entrar(client, password="mala")

    assert _entrar(client).status_code == 200
    assert sesion_bd.query(IntentoAcceso).count() == 0
    for _ in range(4):
        assert _entrar(client, password="mala").status_code == 401  # vuelve a tener 4 intentos antes del bloqueo


def test_los_fallos_viejos_no_se_suman_a_uno_nuevo(client, reloj):
    registrar(client)
    for _ in range(4):
        _entrar(client, password="mala")

    reloj.avanzar(minutes=16)

    assert _entrar(client, password="mala").status_code == 401
    assert _entrar(client, password="mala").status_code == 401  # solo lleva 2, no 6


def test_un_correo_que_no_existe_tambien_se_bloquea_y_no_guarda_el_correo(client, reloj, sesion_bd):
    for _ in range(5):
        r = _entrar(client, email="nadie@example.com", password="x")

    assert r.status_code == 429
    claves = [i.clave for i in sesion_bd.query(IntentoAcceso).all()]
    assert claves and all("nadie" not in c for c in claves)


def test_el_bloqueo_es_por_correo_y_no_afecta_a_otro_cliente(client, reloj):
    registrar(client)
    registrar(client, numero_documento="200300400", email="beto@example.com", celular="3109876543")
    for _ in range(5):
        _entrar(client, password="mala")

    assert _entrar(client).status_code == 429
    assert _entrar(client, email="beto@example.com").status_code == 200


def test_la_contrasena_no_aparece_en_los_logs(client, caplog):
    registrar(client)
    caplog.set_level("INFO")

    _entrar(client, password="Secreta1234")
    _entrar(client)

    texto = caplog.text
    assert "Secreta1234" not in texto and CONTRASENA not in texto and EMAIL not in texto


def test_activar_biometria_comparte_el_limite_de_intentos(client, reloj, sesion_bd):
    cliente_id, auth = registrar(client)
    sesion_bd.get(Cliente, cliente_id).estado_kyc = "VERIFICADO"
    sesion_bd.commit()

    def activar(contrasena):
        return client.post(
            f"/v1/clientes/{cliente_id}/biometria/activar",
            json={"metodo": "HUELLA", "contrasena": contrasena},
            headers=auth,
        )

    for _ in range(4):
        assert activar("mala").status_code == 403
    assert activar("mala").status_code == 429
    assert activar(CONTRASENA).status_code == 429
    reloj.avanzar(minutes=15, seconds=1)
    assert activar(CONTRASENA).status_code == 200
