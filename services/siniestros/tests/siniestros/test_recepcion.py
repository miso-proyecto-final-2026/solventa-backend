"""Recepción por HTTP: acuse, validación, autenticación e idempotencia (criterios 1 a 5 y 8)."""

import pytest

from app.siniestros.servicio import TOPIC_EVENTOS
from tests.siniestros.conftest import CLAVE, evento


def test_un_evento_valido_se_registra_se_publica_y_se_acusa(entorno):
    r = entorno.enviar()

    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["estado"] == "recibido" and cuerpo["duplicado"] is False
    assert cuerpo["evento_id"] == "evt-1"
    assert entorno.resumen() == {
        "recibidos": 1,
        "rechazados": 0,
        "publicados": 1,
        "procesados": 0,
        "reentregas": 0,
    }


def test_el_acuse_no_espera_el_procesamiento(entorno):
    entorno.enviar()

    assert entorno.resumen()["procesados"] == 0  # nadie consumió y aun así respondió 202


def test_el_evento_conserva_fecha_de_recepcion_fuente_y_estado_de_validacion(entorno):
    entorno.enviar()

    from sqlalchemy import select

    from app.siniestros.modelos_db import EventoParametricoDB

    with entorno.repo._fabrica() as s:
        fila = s.scalars(select(EventoParametricoDB)).one()
    assert fila.recibido_en is not None
    assert fila.fuente == "proveedor-sismos"
    assert fila.estado_validacion == "valido"


def test_se_publica_el_sobre_estandar_en_el_topic_con_la_poliza_como_clave(entorno):
    entorno.enviar(evento(poliza_id="pol-9"))

    [(topic, clave, sobre)] = entorno.publicador.publicados
    assert (topic, clave) == (TOPIC_EVENTOS, "pol-9")
    assert sobre.event_type == "evento_parametrico_recibido" and sobre.source == "ms-siniestros"
    assert sobre.payload["tipo"] == "sismo" and sobre.payload["datos"]["magnitud"] == 6.4


def test_sin_poliza_la_clave_de_particion_es_el_id_del_evento(entorno):
    entorno.enviar()

    assert entorno.publicador.publicados[0][1] == "evt-1"


def test_el_sobre_lleva_la_correlacion_de_la_solicitud(entorno):
    entorno.enviar(cabeceras={**entorno.cabeceras, "X-Correlation-Id": "corr-123"})

    assert entorno.publicador.publicados[0][2].correlation_id == "corr-123"


@pytest.mark.parametrize(
    "cuerpo",
    [
        evento(tipo=None),
        evento(datos=None),
        evento(tipo="meteorito"),
        evento(ocurrido_en="no-es-fecha"),
        [1, 2, 3],
    ],
)
def test_un_evento_mal_formado_da_400_se_registra_el_rechazo_y_no_se_publica(entorno, cuerpo):
    r = entorno.enviar(cuerpo)

    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "EVENTO_INVALIDO" and error["trace_id"] and error["details"]
    resumen = entorno.resumen()
    assert resumen["rechazados"] == 1 and resumen["recibidos"] == 0 and resumen["publicados"] == 0


def test_un_json_malformado_da_400_y_deja_rechazo(entorno):
    r = entorno.cliente.post(
        "/v1/siniestros/eventos",
        content=b"{no es json",
        headers={**entorno.cabeceras, "Content-Type": "application/json"},
    )

    assert r.status_code == 400
    assert entorno.resumen()["rechazados"] == 1


def test_una_fuente_desconocida_da_400_con_su_propio_codigo(entorno):
    r = entorno.enviar(evento(fuente="impostor"))

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "FUENTE_DESCONOCIDA"
    assert entorno.resumen()["rechazados"] == 1


@pytest.mark.parametrize("cabeceras", [{}, {"X-Service-Key": "incorrecta"}])
def test_sin_llave_o_con_llave_invalida_da_401_y_no_registra_nada(entorno, cabeceras):
    r = entorno.cliente.post("/v1/siniestros/eventos", json=evento(), headers=cabeceras)

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NO_AUTENTICADO"
    resumen = entorno.resumen()
    assert resumen["recibidos"] == 0 and resumen["rechazados"] == 0


def test_sin_llaves_configuradas_nadie_entra(entorno):
    entorno.app.state.api_keys = {}

    assert entorno.enviar().status_code == 401


def test_el_mismo_evento_dos_veces_no_crea_dos_registros_ni_dos_publicaciones(entorno):
    primero = entorno.enviar()
    segundo = entorno.enviar()

    assert primero.status_code == 202 and segundo.status_code == 200
    assert segundo.json()["duplicado"] is True
    assert segundo.json()["evento_uuid"] == primero.json()["evento_uuid"]
    resumen = entorno.resumen()
    assert resumen["recibidos"] == 1 and resumen["publicados"] == 1
    assert len(entorno.publicador.publicados) == 1


def test_eventos_distintos_con_el_mismo_id_de_otra_fuente_no_chocan(entorno):
    # La unicidad es por (fuente, evento_id): dos proveedores pueden usar el mismo identificador.
    entorno.app.state.api_keys = {CLAVE: "proveedor-sismos", "otra": "proveedor-vuelos"}
    a = entorno.enviar()
    b = entorno.cliente.post(
        "/v1/siniestros/eventos",
        json=evento(fuente="proveedor-vuelos", tipo="vuelo_retrasado"),
        headers={"X-Service-Key": "otra"},
    )

    assert a.status_code == 202 and b.status_code == 202
    assert entorno.resumen()["recibidos"] == 2


def test_la_correlacion_viaja_en_la_respuesta(entorno):
    r = entorno.enviar(cabeceras={**entorno.cabeceras, "X-Correlation-Id": "corr-123"})

    assert r.headers["X-Correlation-Id"] == "corr-123"


def test_con_kafka_caido_responde_503_y_el_reenvio_completa_la_publicacion(entorno):
    entorno.publicador.caido = True

    caido = entorno.enviar(evento(evento_id="b"))

    assert caido.status_code == 503
    assert caido.headers["Retry-After"] == "1"
    assert caido.json()["error"]["code"] == "BUS_SATURADO"
    assert entorno.resumen()["recibidos"] == 1 and entorno.resumen()["publicados"] == 0  # no se pierde

    entorno.publicador.caido = False
    reenvio = entorno.enviar(evento(evento_id="b"))

    assert reenvio.status_code == 200 and reenvio.json()["duplicado"] is True
    resumen = entorno.resumen()
    assert resumen["recibidos"] == 1 and resumen["publicados"] == 1
    assert len(entorno.publicador.publicados) == 1
