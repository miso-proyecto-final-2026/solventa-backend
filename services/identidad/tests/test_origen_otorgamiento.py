"""Origen (IP y user agent) en la auditoría del otorgamiento (HU06): el mismo que ya guarda la revocación (HU07)."""

import pytest

from app.main import app
from app.models import ConsentimientoRegistro
from app.routers.clientes import get_db as get_db_dependencia
from tests.helpers import registrar

TIPO = "OPEN_FINANCE_OPEN_DATA"
CUERPO = {"tipo": TIPO, "version_texto": "v1", "canal": "MOVIL"}


def _bd():
    return next(app.dependency_overrides[get_db_dependencia]())


def _registros():
    return _bd().query(ConsentimientoRegistro).order_by(ConsentimientoRegistro.fecha).all()


def _otorgar(client, cliente_id, auth, **cabeceras):
    return client.post(f"/v1/clientes/{cliente_id}/consentimientos", json=CUERPO, headers={**auth, **cabeceras})


def _revocar(client, cliente_id, auth):
    return client.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers=auth)


def test_otorgar_guarda_la_ip_del_visitante_y_el_user_agent(client):
    cliente_id, auth = registrar(client)

    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "186.29.148.224:54460", "User-Agent": "okhttp/4.12.0"})

    registro = _registros()[0]
    assert (registro.evento, registro.ip_origen, registro.user_agent) == ("OTORGADO", "186.29.148.224", "okhttp/4.12.0")


def test_otorgar_usa_x_forwarded_for_si_no_hay_cloudfront(client):
    cliente_id, auth = registrar(client)

    _otorgar(client, cliente_id, auth, **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})

    assert _registros()[0].ip_origen == "203.0.113.7"


@pytest.mark.parametrize("valor", ["no-es-una-ip", "<script>", "999.1.1.1:80"])
def test_un_valor_que_no_es_una_ip_se_descarta(client, valor):
    cliente_id, auth = registrar(client)

    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": valor})

    assert _registros()[0].ip_origen is None


def test_un_user_agent_enorme_se_recorta_a_500(client):
    cliente_id, auth = registrar(client)

    _otorgar(client, cliente_id, auth, **{"User-Agent": "x" * 2000})

    assert len(_registros()[0].user_agent) == 500


def test_repetir_el_otorgamiento_no_crea_otro_registro_ni_cambia_el_origen(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "186.29.148.224:1", "User-Agent": "primero"})

    repetido = _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "203.0.113.9:2", "User-Agent": "segundo"})

    assert repetido.status_code == 200
    registros = _registros()
    assert len(registros) == 1
    assert (registros[0].ip_origen, registros[0].user_agent) == ("186.29.148.224", "primero")


def test_volver_a_otorgar_guarda_el_origen_del_nuevo_otorgamiento(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "186.29.148.224:1", "User-Agent": "app-a"})
    _revocar(client, cliente_id, auth)

    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "203.0.113.9:2", "User-Agent": "app-b"})

    otorgamiento, revocacion, nuevo = _registros()
    assert (otorgamiento.evento, revocacion.evento, nuevo.evento) == ("OTORGADO", "REVOCADO", "OTORGADO")
    assert (otorgamiento.ip_origen, nuevo.ip_origen) == ("186.29.148.224", "203.0.113.9")
    assert (otorgamiento.user_agent, nuevo.user_agent) == ("app-a", "app-b")


def test_el_otorgamiento_con_origen_sigue_siendo_inmutable(client):
    cliente_id, auth = registrar(client)
    _otorgar(client, cliente_id, auth, **{"CloudFront-Viewer-Address": "186.29.148.224:1"})
    sesion = _bd()
    registro = sesion.query(ConsentimientoRegistro).one()

    registro.ip_origen = "1.1.1.1"
    with pytest.raises(ValueError, match="inmutable"):
        sesion.flush()
