"""HU07 — Revocar el consentimiento de datos: transición, idempotencia, auditoría y permisos."""

import pytest

from app.main import app
from app.models import Consentimiento, ConsentimientoRegistro
from app.routers.clientes import get_db as get_db_dependencia
from tests.helpers import registrar, registrar_segundo

TIPO = "OPEN_FINANCE_OPEN_DATA"
CLAVE_SERVICIO = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", CLAVE_SERVICIO)


def _bd():
    return next(app.dependency_overrides[get_db_dependencia]())


def _otorgar(client, cliente_id, auth, version="v1"):
    return client.post(
        f"/v1/clientes/{cliente_id}/consentimientos",
        json={"tipo": TIPO, "version_texto": version, "canal": "MOVIL"},
        headers=auth,
    )


def _revocar(client, cliente_id, auth, headers=None):
    return client.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers=headers or auth)


def _consultar(client, cliente_id, auth):
    return client.get(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers=auth).json()


def _registros():
    return _bd().query(ConsentimientoRegistro).order_by(ConsentimientoRegistro.fecha).all()


def test_revocar_pasa_de_otorgado_a_revocado_con_fecha(client):
    cliente_id, auth = registrar(client)
    otorgado = _otorgar(client, cliente_id, auth, version="v2").json()

    r = _revocar(client, cliente_id, auth)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == "REVOCADO"
    assert cuerpo["tipo"] == TIPO
    assert cuerpo["fecha_revocacion"]
    assert cuerpo["fecha_otorgamiento"] == otorgado["fecha_otorgamiento"]
    assert cuerpo["version_texto"] == "v2"
    assert _consultar(client, cliente_id, auth)["estado"] == "REVOCADO"


def test_la_respuesta_no_incluye_datos_financieros_ni_personales(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    cuerpo = _revocar(client, cliente_id, auth).json()

    assert set(cuerpo) <= {
        "id", "cliente_id", "tipo", "estado", "version_texto", "fecha_otorgamiento", "fecha_revocacion",
    }


def test_revocar_deja_un_registro_de_auditoria_con_ip_y_user_agent(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    _revocar(
        client,
        cliente_id,
        auth,
        headers={**auth, "User-Agent": "SolventaApp/1.0 (Android 14)", "CloudFront-Viewer-Address": "203.0.113.7:5000"},
    )

    otorgamiento, revocacion = _registros()
    assert (otorgamiento.evento, revocacion.evento) == ("OTORGADO", "REVOCADO")
    assert revocacion.cliente_id == cliente_id
    assert revocacion.tipo == TIPO
    assert revocacion.canal == "MOVIL"
    assert revocacion.user_agent == "SolventaApp/1.0 (Android 14)"
    assert revocacion.ip_origen == "203.0.113.7"
    assert revocacion.fecha is not None


def _ip_registrada(client, headers_extra):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    _revocar(client, cliente_id, auth, headers={**auth, **headers_extra})
    return _registros()[-1].ip_origen


def test_la_ip_de_origen_es_la_del_visitante_que_informa_cloudfront(client):
    assert _ip_registrada(client, {"CloudFront-Viewer-Address": "186.29.148.224:54460"}) == "186.29.148.224"


def test_cloudfront_tambien_informa_ipv6_con_y_sin_corchetes(client):
    assert _ip_registrada(client, {"CloudFront-Viewer-Address": "[2001:db8::7]:443"}) == "2001:db8::7"


def test_cloudfront_tiene_prioridad_sobre_x_forwarded_for(client):
    ip = _ip_registrada(
        client,
        {"CloudFront-Viewer-Address": "186.29.148.224:54460", "X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )

    assert ip == "186.29.148.224"


def test_sin_cloudfront_se_usa_la_primera_de_x_forwarded_for(client):
    assert _ip_registrada(client, {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}) == "203.0.113.7"


@pytest.mark.parametrize("valor", ["no-es-una-ip", "<script>", "999.1.1.1:80", ""])
def test_un_valor_que_no_es_una_ip_se_descarta_y_se_usa_la_del_socket(client, valor):
    ip = _ip_registrada(client, {"CloudFront-Viewer-Address": valor, "X-Forwarded-For": valor})

    # El cliente de pruebas tampoco tiene una IP de socket válida ("testclient"): no se inventa ninguna.
    assert ip is None


def test_un_user_agent_enorme_se_recorta(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    _revocar(client, cliente_id, auth, headers={**auth, "User-Agent": "x" * 2000})

    assert len(_registros()[-1].user_agent) == 500


def test_el_canal_se_toma_del_parametro(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    r = client.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}?canal=WEB", headers=auth)

    assert r.status_code == 200
    assert _registros()[-1].canal == "WEB"


def test_un_canal_invalido_es_422(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    r = client.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}?canal=FAX", headers=auth)

    assert r.status_code == 422


def test_revocar_dos_veces_responde_el_estado_actual_sin_otro_registro(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    primera = _revocar(client, cliente_id, auth)

    segunda = _revocar(client, cliente_id, auth)

    assert segunda.status_code == 200
    assert segunda.json() == primera.json()
    assert segunda.json()["fecha_revocacion"] == primera.json()["fecha_revocacion"]
    assert [r.evento for r in _registros()] == ["OTORGADO", "REVOCADO"]


def test_revocar_sin_haber_otorgado_es_404_y_no_deja_rastro(client):
    cliente_id, auth = registrar(client)

    r = _revocar(client, cliente_id, auth)

    assert r.status_code == 404
    assert _registros() == []
    assert _bd().query(Consentimiento).count() == 0


def test_un_cliente_inexistente_es_404(client):
    cliente_id, auth = registrar(client)

    r = client.delete(f"/v1/clientes/00000000-0000-0000-0000-000000000000/consentimientos/{TIPO}", headers=auth)

    assert r.status_code in (403, 404)
    assert cliente_id


def test_sin_token_es_401(client):
    cliente_id, _ = registrar(client)

    assert client.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}").status_code == 401


def test_un_cliente_no_puede_revocar_el_consentimiento_de_otro(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    _, auth_otro = registrar_segundo(client)

    r = _revocar(client, cliente_id, auth_otro)

    assert r.status_code == 403
    assert _consultar(client, cliente_id, auth)["estado"] == "OTORGADO"
    assert len(_registros()) == 1


def test_un_servicio_no_puede_revocar_en_nombre_del_cliente(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)

    r = client.delete(
        f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers={"X-Service-Key": CLAVE_SERVICIO}
    )

    assert r.status_code in (401, 403)
    assert _consultar(client, cliente_id, auth)["estado"] == "OTORGADO"


def test_se_puede_volver_a_otorgar_y_revocar_y_todo_queda_en_la_auditoria(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    _revocar(client, cliente_id, auth)

    nuevo = _otorgar(client, cliente_id, auth, version="v2")

    assert nuevo.json()["estado"] == "OTORGADO"
    assert nuevo.json()["fecha_revocacion"] is None
    segunda_revocacion = _revocar(client, cliente_id, auth)
    assert segunda_revocacion.json()["estado"] == "REVOCADO"
    assert [r.evento for r in _registros()] == ["OTORGADO", "REVOCADO", "OTORGADO", "REVOCADO"]
    assert _bd().query(Consentimiento).count() == 1


def test_el_registro_de_revocacion_tambien_es_inmutable(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth)
    _revocar(client, cliente_id, auth)
    sesion = _bd()
    revocacion = sesion.query(ConsentimientoRegistro).filter_by(evento="REVOCADO").one()

    revocacion.ip_origen = "1.1.1.1"
    with pytest.raises(ValueError, match="inmutable"):
        sesion.flush()
