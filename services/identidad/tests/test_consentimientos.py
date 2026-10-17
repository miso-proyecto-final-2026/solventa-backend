import pytest

from app.main import app
from app.models import Consentimiento, ConsentimientoRegistro
from app.routers.clientes import get_db as get_db_dependencia

TIPO = "OPEN_FINANCE_OPEN_DATA"
CLAVE_SERVICIO = "clave-de-prueba"


def _registrar(client, **overrides):
    payload = {
        "nombre": "Ana",
        "apellido": "Pérez",
        "tipo_documento": "CC",
        "numero_documento": "100200300",
        "email": "ana.perez@example.com",
        "celular": "3001234567",
        "password": "Clave1234",
        "acepto_terminos": True,
        **overrides,
    }
    cuerpo = client.post("/v1/clientes", json=payload).json()
    return cuerpo["id"], {"Authorization": f"Bearer {cuerpo['access_token']}"}


def _otorgar(client, cliente_id, headers, **overrides):
    return client.post(
        f"/v1/clientes/{cliente_id}/consentimientos",
        json={"tipo": TIPO, "version_texto": "v1", "canal": "MOVIL", **overrides},
        headers=headers,
    )


def _consultar(client, cliente_id, headers):
    return client.get(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers=headers)


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", f"{CLAVE_SERVICIO},otra-clave")


def _sesion_bd():
    return next(app.dependency_overrides[get_db_dependencia]())


def test_sin_otorgar_el_estado_es_no_otorgado(client):
    cliente_id, auth = _registrar(client)

    r = _consultar(client, cliente_id, auth)

    assert r.status_code == 200
    assert r.json()["estado"] == "NO_OTORGADO"
    assert r.json()["version_texto"] is None


def test_otorgar_registra_estado_version_y_fecha(client):
    cliente_id, auth = _registrar(client)

    r = _otorgar(client, cliente_id, auth, version_texto="v2")

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["estado"] == "OTORGADO"
    assert cuerpo["tipo"] == TIPO
    assert cuerpo["version_texto"] == "v2"
    assert cuerpo["fecha_otorgamiento"]
    consulta = _consultar(client, cliente_id, auth).json()
    assert consulta["estado"] == "OTORGADO"
    assert consulta["version_texto"] == "v2"


def test_otorgar_dos_veces_no_duplica_el_consentimiento_vigente(client):
    cliente_id, auth = _registrar(client)

    primero = _otorgar(client, cliente_id, auth)
    reintento = _otorgar(client, cliente_id, auth)

    assert primero.status_code == 201
    assert reintento.status_code == 200
    assert reintento.json()["fecha_otorgamiento"] == primero.json()["fecha_otorgamiento"]
    sesion = _sesion_bd()
    assert sesion.query(Consentimiento).count() == 1
    assert sesion.query(ConsentimientoRegistro).count() == 1


def test_cada_otorgamiento_deja_un_registro_con_canal(client):
    cliente_id, auth = _registrar(client)

    _otorgar(client, cliente_id, auth, version_texto="v1", canal="MOVIL")

    registro = _sesion_bd().query(ConsentimientoRegistro).one()
    assert registro.cliente_id == cliente_id
    assert registro.tipo == TIPO
    assert registro.version_texto == "v1"
    assert registro.canal == "MOVIL"
    assert registro.fecha is not None


def test_el_registro_de_auditoria_es_inmutable(client):
    cliente_id, auth = _registrar(client)
    _otorgar(client, cliente_id, auth)
    sesion = _sesion_bd()
    registro = sesion.query(ConsentimientoRegistro).one()

    registro.version_texto = "v9"
    with pytest.raises(ValueError, match="inmutable"):
        sesion.flush()
    sesion.rollback()

    registro = sesion.query(ConsentimientoRegistro).one()
    sesion.delete(registro)
    with pytest.raises(ValueError, match="inmutable"):
        sesion.flush()


def test_estado_revocado_se_consulta_y_permite_volver_a_otorgar(client):
    # La revocación es HU07; aquí se verifica que el contrato de consulta ya cubre REVOCADO.
    cliente_id, auth = _registrar(client)
    _otorgar(client, cliente_id, auth)
    sesion = _sesion_bd()
    c = sesion.query(Consentimiento).one()
    c.estado = "REVOCADO"
    sesion.commit()

    assert _consultar(client, cliente_id, auth).json()["estado"] == "REVOCADO"

    r = _otorgar(client, cliente_id, auth, version_texto="v2")
    assert r.status_code == 201
    assert r.json()["estado"] == "OTORGADO"
    assert r.json()["version_texto"] == "v2"
    assert _sesion_bd().query(ConsentimientoRegistro).count() == 2


@pytest.mark.parametrize("version", ["1", "version1", "v", "v1.0", ""])
def test_rechaza_version_de_texto_invalida(client, version):
    cliente_id, auth = _registrar(client)

    r = _otorgar(client, cliente_id, auth, version_texto=version)

    assert r.status_code == 422


def test_rechaza_tipo_desconocido(client):
    cliente_id, auth = _registrar(client)

    assert _otorgar(client, cliente_id, auth, tipo="OTRO").status_code == 422


def test_otorgar_sin_sesion_es_401(client):
    cliente_id, _ = _registrar(client)

    assert _otorgar(client, cliente_id, {}).status_code == 401
    assert _otorgar(client, cliente_id, {"Authorization": "Bearer falso"}).status_code == 401


def test_un_cliente_no_otorga_ni_consulta_el_consentimiento_de_otro(client):
    ana_id, ana = _registrar(client)
    _, beto = _registrar(
        client, numero_documento="200300400", email="beto@example.com", celular="3109876543"
    )
    _otorgar(client, ana_id, ana)

    assert _otorgar(client, ana_id, beto).status_code == 403
    assert _consultar(client, ana_id, beto).status_code == 403


def test_un_servicio_con_clave_valida_consulta_el_estado(client):
    cliente_id, auth = _registrar(client)
    _otorgar(client, cliente_id, auth)

    r = _consultar(client, cliente_id, {"X-Service-Key": CLAVE_SERVICIO})

    assert r.status_code == 200
    assert r.json()["estado"] == "OTORGADO"


def test_clave_de_servicio_invalida_es_401(client):
    cliente_id, _ = _registrar(client)

    assert _consultar(client, cliente_id, {"X-Service-Key": "mala"}).status_code == 401


def test_sin_claves_configuradas_ninguna_clave_de_servicio_es_valida(client, monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEYS")
    cliente_id, _ = _registrar(client)

    assert _consultar(client, cliente_id, {"X-Service-Key": ""}).status_code == 401


def test_la_clave_de_servicio_no_sirve_para_otorgar(client):
    cliente_id, _ = _registrar(client)

    r = _otorgar(client, cliente_id, {"X-Service-Key": CLAVE_SERVICIO})

    assert r.status_code == 403


def test_cliente_inexistente_es_404_para_un_servicio(client):
    r = client.get(
        f"/v1/clientes/no-existe/consentimientos/{TIPO}",
        headers={"X-Service-Key": CLAVE_SERVICIO},
    )

    assert r.status_code == 404


def test_el_token_de_registro_no_se_guarda_en_claro(client):
    from app.models import Sesion

    cliente_id, auth = _registrar(client)
    token = auth["Authorization"].removeprefix("Bearer ")

    sesion = _sesion_bd().query(Sesion).one()

    assert sesion.cliente_id == cliente_id
    assert sesion.token_hash != token
    assert len(sesion.token_hash) == 64


def test_contrato_de_consulta_que_consume_cotizacion(client):
    """Cotización (ConsentimientoHttp) depende de esta forma exacta; cambiarla rompe su contrato."""
    cliente_id, auth = _registrar(client)
    _otorgar(client, cliente_id, auth)

    cuerpo = _consultar(client, cliente_id, {"X-Service-Key": CLAVE_SERVICIO}).json()

    assert set(cuerpo) == {
        "id",  # HU11: la usa Cotización para la trazabilidad (campo aditivo)
        "cliente_id",
        "tipo",
        "estado",
        "version_texto",
        "fecha_otorgamiento",
        "fecha_revocacion",
    }
    assert cuerpo["estado"] in {"OTORGADO", "REVOCADO", "NO_OTORGADO"}


def test_el_consentimiento_vigente_expone_su_id_y_sin_otorgar_no_hay_id(client):
    cliente_id, auth = _registrar(client)
    sin_otorgar = _consultar(client, cliente_id, auth).json()

    otorgado = _otorgar(client, cliente_id, auth).json()
    consultado = _consultar(client, cliente_id, auth).json()
    repetido = _otorgar(client, cliente_id, auth).json()

    assert sin_otorgar["id"] is None
    assert otorgado["id"] and otorgado["id"] == consultado["id"] == repetido["id"]
    with _sesion_bd() as db:
        assert db.query(Consentimiento).one().id == otorgado["id"]
