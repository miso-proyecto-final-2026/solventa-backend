"""HU04 — acceso biométrico: el servidor solo registra qué método se activó (la verificación es local)."""

from app.models import Cliente, RegistroBiometria
from tests.helpers import PAYLOAD_CLIENTE, registrar, registrar_segundo

CONTRASENA = PAYLOAD_CLIENTE["password"]


def _aprobar_kyc(sesion_bd, cliente_id):
    sesion_bd.get(Cliente, cliente_id).estado_kyc = "VERIFICADO"
    sesion_bd.commit()


def _activar(client, cliente_id, auth, metodo="HUELLA", contrasena=CONTRASENA):
    return client.post(
        f"/v1/clientes/{cliente_id}/biometria/activar",
        json={"metodo": metodo, "contrasena": contrasena},
        headers=auth,
    )


def test_sin_activar_el_estado_es_inactivo(client):
    cliente_id, auth = registrar(client)

    r = client.get(f"/v1/clientes/{cliente_id}/biometria", headers=auth)

    assert r.status_code == 200
    assert r.json() == {
        "cliente_id": cliente_id, "activo": False, "metodo": None, "activado_en": None, "desactivado_en": None,
    }


def test_activa_con_kyc_aprobado_y_contrasena_correcta(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)

    r = _activar(client, cliente_id, auth, metodo="FACIAL")

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["activo"] is True and cuerpo["metodo"] == "FACIAL" and cuerpo["activado_en"]
    assert client.get(f"/v1/clientes/{cliente_id}/biometria", headers=auth).json()["metodo"] == "FACIAL"


def test_kyc_no_aprobado_no_deja_activar(client):
    cliente_id, auth = registrar(client)

    r = _activar(client, cliente_id, auth)

    assert r.status_code == 403
    assert r.json()["detail"]["codigo"] == "KYC_NO_APROBADO"


def test_contrasena_incorrecta_no_activa_y_no_guarda_nada(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)

    r = _activar(client, cliente_id, auth, contrasena="Otra12345")

    assert r.status_code == 403
    assert r.json()["detail"]["codigo"] == "CONTRASENA_INCORRECTA"
    assert sesion_bd.query(RegistroBiometria).count() == 0


def test_metodo_desconocido_es_422(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)

    assert _activar(client, cliente_id, auth, metodo="IRIS").status_code == 422


def test_cambiar_de_metodo_reutiliza_la_fila(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)

    _activar(client, cliente_id, auth, metodo="HUELLA")
    r = _activar(client, cliente_id, auth, metodo="FACIAL")

    assert r.json()["metodo"] == "FACIAL"
    assert sesion_bd.query(RegistroBiometria).count() == 1


def test_desactivar_deja_el_registro_inactivo_y_es_idempotente(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)
    _activar(client, cliente_id, auth)

    assert client.delete(f"/v1/clientes/{cliente_id}/biometria", headers=auth).status_code == 204
    assert client.delete(f"/v1/clientes/{cliente_id}/biometria", headers=auth).status_code == 204

    estado = client.get(f"/v1/clientes/{cliente_id}/biometria", headers=auth).json()
    assert estado["activo"] is False and estado["metodo"] is None and estado["desactivado_en"]


def test_desactivar_sin_haber_activado_es_204(client):
    cliente_id, auth = registrar(client)

    assert client.delete(f"/v1/clientes/{cliente_id}/biometria", headers=auth).status_code == 204


def test_se_puede_reactivar_tras_desactivar(client, sesion_bd):
    cliente_id, auth = registrar(client)
    _aprobar_kyc(sesion_bd, cliente_id)
    _activar(client, cliente_id, auth)
    client.delete(f"/v1/clientes/{cliente_id}/biometria", headers=auth)

    r = _activar(client, cliente_id, auth, metodo="FACIAL")

    assert r.json()["activo"] is True and r.json()["desactivado_en"] is None


def test_sin_sesion_es_401(client):
    cliente_id, _ = registrar(client)

    assert client.get(f"/v1/clientes/{cliente_id}/biometria").status_code == 401
    assert client.delete(f"/v1/clientes/{cliente_id}/biometria").status_code == 401


def test_un_cliente_no_toca_la_biometria_de_otro(client, sesion_bd):
    cliente_id, _ = registrar(client)
    _, auth_otro = registrar_segundo(client)
    _aprobar_kyc(sesion_bd, cliente_id)

    assert _activar(client, cliente_id, auth_otro).status_code == 403
    assert client.delete(f"/v1/clientes/{cliente_id}/biometria", headers=auth_otro).status_code == 403
