"""Contrato HTTP de Suscripción (criterios 1, 6, 7 y 9)."""

import asyncio

import pytest
from app.suscripcion.api import router
from app.suscripcion.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.dobles import CotizacionesFalsas, cotizacion

CABECERAS = {"Authorization": "Bearer token-1", "Idempotency-Key": "k-1"}
CUERPO = {"cotizacion_id": "cot-1", "metodo_pago": "tok_visa_123", "acepta_terminos": True}


@pytest.fixture
async def cliente(armar):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_suscripcion = armar()
    app.state.tareas = set()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.app = app
        yield c


async def _esperar_tareas(cliente):
    await asyncio.gather(*cliente.app.state.tareas)


async def test_post_responde_202_con_la_saga_en_la_decision(cliente):
    r = await cliente.post("/v1/suscripciones", json=CUERPO, headers=CABECERAS)
    assert r.status_code == 202
    cuerpo = r.json()
    assert (cuerpo["estado_saga"], cuerpo["paso_actual"]) == ("EN_PROGRESO", "DECISION")
    assert cuerpo["pasos"] == ["DECISION", "COBRO", "FIRMA", "EMISION"]
    assert cuerpo["decision"] is None
    assert "metodo_pago" not in r.text and "tok_visa_123" not in r.text


async def test_get_muestra_la_decision_cuando_termina(cliente):
    r = await cliente.post("/v1/suscripciones", json=CUERPO, headers=CABECERAS)
    await _esperar_tareas(cliente)
    g = await cliente.get(f"/v1/suscripciones/{r.json()['id']}", headers=CABECERAS)
    assert g.status_code == 200
    assert (g.json()["estado_saga"], g.json()["paso_actual"]) == ("APROBADA", "COBRO")
    assert g.json()["decision"]["decision"] == "APROBADA"
    assert g.json()["decision"]["reglas_version"]


async def test_repetir_devuelve_la_misma_saga_y_lo_avisa(cliente):
    a = await cliente.post("/v1/suscripciones", json=CUERPO, headers=CABECERAS)
    b = await cliente.post("/v1/suscripciones", json=CUERPO, headers=CABECERAS)
    assert a.json()["id"] == b.json()["id"]
    assert b.status_code == 202 and b.headers["Idempotent-Replay"] == "true"
    assert "Idempotent-Replay" not in a.headers


@pytest.mark.parametrize(
    ("cambio", "estado", "codigo"),
    [
        ({"headers": {"Idempotency-Key": "k"}}, 401, "NO_AUTENTICADO"),
        ({"headers": {"Authorization": "Bearer malo", "Idempotency-Key": "k"}}, 401, "NO_AUTENTICADO"),
        ({"headers": {"Authorization": "Bearer token-1"}}, 422, "VALIDATION_ERROR"),
        ({"json": {**CUERPO, "acepta_terminos": False}}, 422, "VALIDATION_ERROR"),
        ({"json": {"cotizacion_id": "cot-1"}}, 422, "VALIDATION_ERROR"),
        ({"json": {**CUERPO, "numero_tarjeta": "4111111111111111"}}, 422, "VALIDATION_ERROR"),
        ({"json": {**CUERPO, "cotizacion_id": "nada"}}, 404, "COTIZACION_NO_ENCONTRADA"),
    ],
)
async def test_errores_con_el_formato_estandar(cliente, cambio, estado, codigo):
    r = await cliente.post(
        "/v1/suscripciones", json=cambio.get("json", CUERPO), headers=cambio.get("headers", CABECERAS)
    )
    assert r.status_code == estado
    error = r.json()["error"]
    assert error["code"] == codigo and error["message"] and error["trace_id"] and isinstance(error["details"], list)
    assert "4111111111111111" not in r.text


async def test_el_error_de_validacion_no_repite_el_valor_recibido(cliente):
    r = await cliente.post("/v1/suscripciones", json={**CUERPO, "numero_tarjeta": "4111111111111111"}, headers=CABECERAS)
    assert "4111" not in r.text


async def test_cotizacion_vencida_y_ya_usada_devuelven_409_propios(armar):
    from datetime import timedelta

    from tests.dobles import AHORA

    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_suscripcion = armar(
        cotizaciones=CotizacionesFalsas(cotizacion(), cotizacion(id="vieja", vigente_hasta=AHORA - timedelta(days=1)))
    )
    app.state.tareas = set()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        vieja = await c.post("/v1/suscripciones", json={**CUERPO, "cotizacion_id": "vieja"}, headers=CABECERAS)
        await c.post("/v1/suscripciones", json=CUERPO, headers=CABECERAS)
        usada = await c.post("/v1/suscripciones", json=CUERPO, headers={**CABECERAS, "Idempotency-Key": "k-2"})
    assert (vieja.status_code, vieja.json()["error"]["code"]) == (409, "COTIZACION_VENCIDA")
    assert (usada.status_code, usada.json()["error"]["code"]) == (409, "COTIZACION_YA_USADA")


async def test_get_de_otra_suscripcion_o_inexistente_es_404(cliente):
    r = await cliente.get("/v1/suscripciones/no-existe", headers=CABECERAS)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "SUSCRIPCION_NO_ENCONTRADA")
    assert (await cliente.get("/v1/suscripciones/x")).status_code == 401
