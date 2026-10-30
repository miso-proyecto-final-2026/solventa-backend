"""HU21 por HTTP: los endpoints que usan por igual la app móvil y el web."""

import pytest
from app.suscripcion.api import router as suscripciones
from app.suscripcion.api_firma import router as firma
from app.suscripcion.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.test_firma import OTP, _lista

TOKEN_1 = {"Authorization": "Bearer token-1"}
TOKEN_2 = {"Authorization": "Bearer token-2"}


def _cuerpo(huella, **cambios):
    return {"documento_sha256": huella, "documento_revisado": True, "codigo": OTP, "canal": "MOVIL", "dispositivo": "Pixel 8", **cambios}


@pytest.fixture
async def escenario(armar, fabrica):
    servicio_firma, susc, almacen, reloj = await _lista(armar, fabrica)
    app = FastAPI()
    app.include_router(suscripciones)
    app.include_router(firma)
    registrar_errores(app)
    app.state.servicio_suscripcion = armar()
    app.state.servicio_firma = servicio_firma
    app.state.tareas = set()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, almacen, reloj


async def test_el_cliente_lee_el_documento_el_pdf_y_firma(escenario):
    api, susc, _almacen, _ = escenario

    documento = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)
    pdf = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma/pdf", headers=TOKEN_1)
    codigo = await api.post(f"/v1/suscripciones/{susc.id}/firma/codigo", headers=TOKEN_1)
    r = await api.post(
        f"/v1/suscripciones/{susc.id}/firma",
        json=_cuerpo(documento.json()["documento_sha256"]),
        headers={**TOKEN_1, "X-Forwarded-For": "190.9.8.7, 10.0.0.2"},
    )

    assert documento.status_code == 200 and documento.json()["estado"] == "PENDIENTE" and documento.json()["autenticacion"] == "OTP"
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert codigo.status_code == 202
    assert r.status_code == 200 and "idempotent-replay" not in r.headers
    assert (r.json()["estado_saga"], r.json()["canal"], r.json()["metodo_autenticacion"]) == ("EMITIDA", "MOVIL", "OTP")
    ev = await api.get(f"/v1/suscripciones/{susc.id}/firma", headers=TOKEN_1)
    assert ev.json()["documento_sha256"] == documento.json()["documento_sha256"]


async def test_la_ip_de_la_evidencia_es_la_primera_de_x_forwarded_for(escenario, fabrica):
    api, susc, *_ = escenario
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]

    await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella), headers={**TOKEN_1, "X-Forwarded-For": "190.9.8.7, 10.0.0.2"})

    from tests.test_firma import _firmas

    assert _firmas(fabrica)[0].ip == "190.9.8.7"


async def test_firmar_otra_vez_responde_la_misma_firma_con_la_marca_de_repeticion(escenario):
    api, susc, *_ = escenario
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]
    primera = await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella), headers=TOKEN_1)

    segunda = await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella, canal="WEB"), headers=TOKEN_1)

    assert segunda.status_code == 200 and segunda.headers["idempotent-replay"] == "true"
    assert segunda.json() == primera.json() and segunda.json()["canal"] == "MOVIL"  # se refleja el primer canal


async def test_la_copia_firmada_se_descarga_como_pdf(escenario):
    api, susc, *_ = escenario
    antes = await api.get(f"/v1/suscripciones/{susc.id}/firma/documento", headers=TOKEN_1)
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]
    await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella), headers=TOKEN_1)

    r = await api.get(f"/v1/suscripciones/{susc.id}/firma/documento", headers=TOKEN_1)

    assert antes.status_code == 404 and antes.json()["error"]["code"] == "FIRMA_NO_ENCONTRADA"
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "firmada.pdf" in r.headers["content-disposition"]


@pytest.mark.parametrize(
    "cambios, estado, codigo",
    [
        ({"codigo": "000000"}, 403, "OTP_INVALIDO"),
        ({"documento_sha256": "f" * 64}, 409, "DOCUMENTO_ALTERADO"),
        ({"documento_revisado": False}, 422, "VALIDATION_ERROR"),
        ({"canal": "FAX"}, 422, "VALIDATION_ERROR"),
        ({"documento_sha256": "no-es-un-hash"}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_los_errores_usan_la_estructura_estandar(escenario, cambios, estado, codigo):
    api, susc, *_ = escenario
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]

    r = await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella, **cambios), headers=TOKEN_1)

    assert r.status_code == estado
    assert r.json()["error"]["code"] == codigo and r.json()["error"]["trace_id"]


async def test_sin_sesion_o_con_la_sesion_de_otro_cliente_no_se_ve_nada(escenario):
    api, susc, *_ = escenario

    sin = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma")
    ajeno = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_2)
    pdf_ajeno = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma/pdf", headers=TOKEN_2)

    assert sin.status_code == 401
    assert ajeno.status_code == pdf_ajeno.status_code == 404


async def test_firma_vencida_responde_409_y_la_saga_muestra_la_compensacion(escenario):
    api, susc, _, reloj = escenario
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]
    from datetime import timedelta

    reloj.ahora += timedelta(hours=24)

    r = await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella), headers=TOKEN_1)
    saga = await api.get(f"/v1/suscripciones/{susc.id}", headers=TOKEN_1)

    assert r.status_code == 409 and r.json()["error"]["code"] == "FIRMA_VENCIDA"
    assert saga.json()["firma"]["estado"] == "VENCIDA" and saga.json()["firma"]["compensacion"] == "SOLICITADA"


async def test_la_saga_muestra_el_bloque_de_firma_pendiente_y_firmada(escenario):
    api, susc, *_ = escenario
    antes = await api.get(f"/v1/suscripciones/{susc.id}", headers=TOKEN_1)
    huella = (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers=TOKEN_1)).json()["documento_sha256"]
    await api.post(f"/v1/suscripciones/{susc.id}/firma", json=_cuerpo(huella), headers=TOKEN_1)

    despues = await api.get(f"/v1/suscripciones/{susc.id}", headers=TOKEN_1)

    assert antes.json()["firma"]["estado"] == "PENDIENTE" and antes.json()["firma"]["vence_en"]
    assert despues.json()["firma"]["estado"] == "FIRMADA" and despues.json()["estado_saga"] == "EMITIDA"
