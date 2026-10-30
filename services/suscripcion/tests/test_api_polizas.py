"""HU20, criterio 5: `GET /v1/suscripciones/{id}/poliza`, la billetera `GET /v1/polizas` y el bloque `poliza` de la saga."""

import pytest
from app.suscripcion.api import router as suscripciones
from app.suscripcion.api_polizas import router as polizas
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.test_cobros import _resultado
from tests.test_polizas import AlmacenMemoria, _cobrada, _generador, _poliza

TOKEN_1 = {"Authorization": "Bearer token-1"}
TOKEN_2 = {"Authorization": "Bearer token-2"}


def _app(servicio) -> FastAPI:
    app = FastAPI()
    app.include_router(suscripciones)
    app.include_router(polizas)
    registrar_errores(app)
    app.state.servicio_suscripcion = servicio
    app.state.tareas = set()
    return app


@pytest.fixture
async def api(armar):
    async with AsyncClient(transport=ASGITransport(app=_app(armar())), base_url="http://t") as c:
        yield c


async def test_la_poliza_de_la_suscripcion_muestra_metadatos_sin_url_de_descarga(api, armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    await _generador(fabrica, AlmacenMemoria()).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)

    r = await api.get(f"/v1/suscripciones/{susc.id}/poliza", headers=TOKEN_1)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["numero_poliza"] == poliza.numero_poliza
    assert (cuerpo["estado"], cuerpo["documento_estado"]) == ("PENDIENTE_FIRMA", "GENERADO")
    assert cuerpo["documento_s3_key"] == f"polizas/{susc.id}/{poliza.numero_poliza}.pdf"
    assert (cuerpo["prima_anual"], cuerpo["prima_mensual"], cuerpo["moneda"]) == (175_000, 14_583, "COP")
    assert cuerpo["vigencia_desde"].startswith("2026-10-25") and cuerpo["fecha_vencimiento"].startswith("2027-10-25")
    assert not any("url" in campo for campo in cuerpo)  # la descarga es de HU22


async def test_una_suscripcion_sin_poliza_responde_404_con_la_estructura_estandar(api, armar):
    susc, _ = await armar().iniciar(token="token-1", clave="k-1", cotizacion_id="cot-1", metodo_pago="tok_x", acepta_terminos=True)

    r = await api.get(f"/v1/suscripciones/{susc.id}/poliza", headers=TOKEN_1)

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "POLIZA_NO_ENCONTRADA"
    assert r.json()["error"]["trace_id"]


async def test_la_poliza_ajena_responde_igual_que_una_inexistente(api, armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    ajena = await api.get(f"/v1/suscripciones/{susc.id}/poliza", headers=TOKEN_2)
    inexistente = await api.get("/v1/suscripciones/no-existe/poliza", headers=TOKEN_2)

    assert ajena.status_code == inexistente.status_code == 404
    assert ajena.json()["error"]["code"] == inexistente.json()["error"]["code"] == "POLIZA_NO_ENCONTRADA"


async def test_sin_token_la_poliza_es_401(api):
    assert (await api.get("/v1/suscripciones/x/poliza")).status_code == 401


async def test_la_billetera_lista_las_polizas_del_cliente_con_estado_vigencia_y_prima(api, armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    r = await api.get("/v1/polizas", params={"cliente_id": "cli-1"}, headers=TOKEN_1)

    assert r.status_code == 200
    (item,) = r.json()["polizas"]
    assert item["suscripcion_id"] == susc.id
    assert (item["estado"], item["prima_anual"], item["prima_mensual"]) == ("PENDIENTE_FIRMA", 175_000, 14_583)
    assert item["vigencia_desde"] and item["fecha_vencimiento"]


async def test_la_billetera_de_un_cliente_sin_polizas_esta_vacia(api):
    r = await api.get("/v1/polizas", params={"cliente_id": "cli-1"}, headers=TOKEN_1)
    assert (r.status_code, r.json()) == (200, {"cliente_id": "cli-1", "polizas": []})


async def test_pedir_la_billetera_de_otro_cliente_es_403_con_la_estructura_estandar(api, armar, fabrica):
    await _cobrada(armar, fabrica)

    r = await api.get("/v1/polizas", params={"cliente_id": "cli-1"}, headers=TOKEN_2)

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCESO_DENEGADO"
    assert r.json()["error"]["trace_id"]


async def test_la_billetera_exige_token_y_cliente_id(api):
    assert (await api.get("/v1/polizas", params={"cliente_id": "cli-1"})).status_code == 401
    assert (await api.get("/v1/polizas", headers=TOKEN_1)).status_code == 422


async def test_la_saga_muestra_el_bloque_poliza_para_el_operador(armar, fabrica):
    servicio = armar()
    susc, _ = await servicio.iniciar(token="token-1", clave="k-1", cotizacion_id="cot-1", metodo_pago="tok_x", acepta_terminos=True)
    await servicio.decidir(susc.id)
    async with AsyncClient(transport=ASGITransport(app=_app(servicio)), base_url="http://t") as c:
        antes = await c.get(f"/v1/suscripciones/{susc.id}", headers=TOKEN_1)
        with fabrica() as s, s.begin():
            await manejar_resultado_cobro(_resultado(susc.id), s)
        despues = await c.get(f"/v1/suscripciones/{susc.id}", headers=TOKEN_1)

    assert antes.json()["poliza"] is None
    bloque = despues.json()["poliza"]
    assert (bloque["estado"], bloque["documento_estado"], bloque["documento_intentos"]) == ("PENDIENTE_FIRMA", "PENDIENTE", 0)
    assert bloque["numero_poliza"] == _poliza(fabrica, susc.id).numero_poliza
