"""HU22 por HTTP: descarga del titular, enlace para el correo y descarga por enlace."""

from datetime import timedelta

import pytest
from app.suscripcion.api import router as suscripciones
from app.suscripcion.api_certificado import router as certificado
from app.suscripcion.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.test_certificado import _emitida
from tests.test_firma import _poliza, _texto

TOKEN_1 = {"Authorization": "Bearer token-1"}
TOKEN_2 = {"Authorization": "Bearer token-2"}
CLAVE_SERVICIO = "clave-de-notificaciones"
SERVICIO = {"X-Service-Key": CLAVE_SERVICIO}


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SUSCRIPCION_SERVICE_KEYS", f"{CLAVE_SERVICIO},otra-clave")


@pytest.fixture
async def escenario(armar, fabrica):
    servicio, susc, _almacen, reloj = await _emitida(armar, fabrica)
    app = FastAPI()
    app.include_router(suscripciones)
    app.include_router(certificado)
    registrar_errores(app)
    app.state.servicio_suscripcion = armar()
    app.state.servicio_certificado = servicio
    app.state.tareas = set()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, _poliza(fabrica, susc.id), reloj


async def test_el_titular_descarga_el_certificado_como_pdf(escenario):
    api, susc, poliza, _ = escenario

    r = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers=TOKEN_1)

    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="certificado-{poliza.numero_poliza}.pdf"'
    assert r.headers["cache-control"] == "no-store"
    assert "Certificado de cobertura" in _texto(r.content)


async def test_el_idioma_sale_de_accept_language(escenario):
    api, susc, *_ = escenario

    en = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers={**TOKEN_1, "Accept-Language": "en-US,en;q=0.9"})
    es = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers={**TOKEN_1, "Accept-Language": "es-CO"})

    assert "Certificate of coverage" in _texto(en.content) and "Certificado de cobertura" in _texto(es.content)


async def test_sin_sesion_es_401_y_el_certificado_de_otro_cliente_es_404(escenario):
    api, susc, *_ = escenario

    sin = await api.get(f"/v1/suscripciones/{susc.id}/certificado")
    ajeno = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers=TOKEN_2)
    inexistente = await api.get("/v1/suscripciones/no-existe/certificado", headers=TOKEN_2)

    assert sin.status_code == 401
    assert ajeno.status_code == inexistente.status_code == 404
    assert ajeno.json()["error"]["code"] == inexistente.json()["error"]["code"] == "SUSCRIPCION_NO_ENCONTRADA"
    assert ajeno.json()["error"]["trace_id"]


async def test_el_servicio_de_notificaciones_pide_un_enlace_y_cualquiera_lo_descarga_sin_sesion(escenario):
    api, _, poliza, _ = escenario

    emitido = await api.post("/v1/interno/certificados/enlaces", json={"poliza_id": poliza.id, "idioma": "en-US"}, headers=SERVICIO)
    ruta = emitido.json()["ruta"]
    descarga = await api.get(ruta)  # sin ninguna cabecera

    assert emitido.status_code == 201 and ruta.startswith("/v1/certificados/") and emitido.json()["vence_en"]
    assert descarga.status_code == 200 and descarga.headers["content-type"] == "application/pdf"
    assert "Certificate of coverage" in _texto(descarga.content)


@pytest.mark.parametrize("cabeceras", [{}, {"X-Service-Key": "clave-incorrecta"}, TOKEN_1])
async def test_pedir_un_enlace_exige_la_clave_de_un_servicio(escenario, cabeceras):
    api, _, poliza, _ = escenario

    r = await api.post("/v1/interno/certificados/enlaces", json={"poliza_id": poliza.id}, headers=cabeceras)

    assert r.status_code == 401 and r.json()["error"]["code"] == "CLAVE_DE_SERVICIO_INVALIDA"


async def test_sin_claves_de_servicio_configuradas_nadie_pide_enlaces(escenario, monkeypatch):
    api, _, poliza, _ = escenario
    monkeypatch.delenv("SUSCRIPCION_SERVICE_KEYS")

    r = await api.post("/v1/interno/certificados/enlaces", json={"poliza_id": poliza.id}, headers=SERVICIO)

    assert r.status_code == 401


async def test_el_enlace_vencido_responde_410_y_uno_alterado_401(escenario):
    api, _, poliza, reloj = escenario
    ruta = (await api.post("/v1/interno/certificados/enlaces", json={"poliza_id": poliza.id}, headers=SERVICIO)).json()["ruta"]

    alterado = await api.get(ruta[:-3] + "xyz")
    reloj.ahora += timedelta(hours=25)
    vencido = await api.get(ruta)

    assert alterado.status_code == 401 and alterado.json()["error"]["code"] == "ENLACE_INVALIDO"
    assert vencido.status_code == 410 and vencido.json()["error"]["code"] == "ENLACE_VENCIDO"
    assert vencido.json()["error"]["trace_id"]


async def test_los_cuerpos_invalidos_del_enlace_se_rechazan(escenario):
    api, _, poliza, _ = escenario

    for cuerpo in ({}, {"poliza_id": poliza.id, "idioma": "fr-FR"}, {"poliza_id": poliza.id, "extra": 1}):
        r = await api.post("/v1/interno/certificados/enlaces", json=cuerpo, headers=SERVICIO)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_un_enlace_para_una_poliza_inexistente_es_404(escenario):
    api, *_ = escenario

    r = await api.post("/v1/interno/certificados/enlaces", json={"poliza_id": "no-existe"}, headers=SERVICIO)

    assert r.status_code == 404 and r.json()["error"]["code"] == "POLIZA_NO_ENCONTRADA"
