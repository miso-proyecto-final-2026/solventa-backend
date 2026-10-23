"""Recepción por HTTP: acuse, validación, autenticación e idempotencia (criterios 1 a 5 y 8)."""

import pytest

from app.siniestros.bus import TOPIC_EVENTOS, BusSaturado
from tests.siniestros.conftest import CLAVE, Entorno, evento


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
        "rezago": 1,
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


def test_el_mensaje_publicado_lleva_el_evento_y_se_particiona_por_poliza(entorno):
    import asyncio

    entorno.enviar(evento(poliza_id="pol-9"))

    [m] = asyncio.run(entorno.bus.leer(TOPIC_EVENTOS, "otro-grupo", 10))
    assert m.clave == "pol-9"
    assert m.valor["tipo"] == "sismo" and m.valor["datos"]["magnitud"] == 6.4


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
    assert resumen["recibidos"] == 1 and resumen["publicados"] == 1 and resumen["rezago"] == 1


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


def test_con_el_bus_saturado_responde_503_y_el_reenvio_completa_la_publicacion():
    e = Entorno(capacidad_rezago=1)
    try:
        assert e.enviar(evento(evento_id="a")).status_code == 202  # llena el bus
        saturado = e.enviar(evento(evento_id="b"))

        assert saturado.status_code == 503
        assert saturado.headers["Retry-After"] == "1"
        assert saturado.json()["error"]["code"] == "BUS_SATURADO"
        assert e.resumen()["recibidos"] == 2 and e.resumen()["publicados"] == 1  # no se pierde

        import asyncio

        asyncio.run(e.consumidor.drenar())  # el consumidor libera espacio
        reenvio = e.enviar(evento(evento_id="b"))

        assert reenvio.status_code == 200 and reenvio.json()["duplicado"] is True
        resumen = e.resumen()
        assert resumen["recibidos"] == 2 and resumen["publicados"] == 2 and resumen["rezago"] == 1
    finally:
        e.engine.dispose()


def test_el_bus_saturado_es_una_excepcion_propia():
    assert issubclass(BusSaturado, Exception)
