"""HU39 por HTTP: el asesor envía el enlace de firma y el cliente firma sin sesión, con el enlace y su código."""

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from app.suscripcion.certificado import FirmadorEnlaces
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.modelos_db import EnlaceFirmaDB
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.test_api_asistida import (
    ASESOR,
    ASESOR_ID,
    OTRO_ASESOR,
    _armar_app,
    _asignar,
    _auditoria,
    _cuerpo,
    _huella,
    _ruta,
)
from tests.test_firma import _firmas, _lista, _saga
from tests.test_polizas import _poliza
from tests.test_servicio import _salidas

BASE = "/v1/suscripciones/enlace-firma"
SERVICIO = {"X-Service-Key": "clave-de-notificaciones"}


@pytest.fixture(autouse=True)
def _claves(monkeypatch):
    monkeypatch.setenv("SUSCRIPCION_SERVICE_KEYS", "clave-de-notificaciones")


@pytest.fixture
async def escenario(armar, fabrica):
    servicio_firma, susc, _almacen, reloj = await _lista(armar, fabrica)
    _asignar(fabrica, susc.id)
    app = _armar_app(armar(), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, reloj


def _enlaces(fabrica) -> list[EnlaceFirmaDB]:
    with fabrica() as s:
        return list(s.execute(select(EnlaceFirmaDB).order_by(EnlaceFirmaDB.creado_en)).scalars())


def _eventos(fabrica):
    return [x.payload for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA and x.payload["event_type"] == "firma_enlace_solicitada"]


async def _enviar(api, susc):
    r = await api.post(_ruta(susc, "/firma/enlace"), headers=ASESOR)
    assert r.status_code == 202, r.text
    return r.json()


async def _token(api, enlace_id, idioma="es-CO"):
    r = await api.post(f"/v1/interno/firma/enlaces/{enlace_id}/token", json={"idioma": idioma}, headers=SERVICIO)
    assert r.status_code == 200, r.text
    return r.json()["ruta"].split("#t=")[1]


async def _enlace_listo(api, susc, fabrica):
    await _enviar(api, susc)
    token = await _token(api, _enlaces(fabrica)[-1].id)
    return {"X-Enlace-Firma": token}


async def _firmar(api, cabecera, **cambios):
    huella = (await api.get(f"{BASE}/documento", headers=cabecera)).json()["documento_sha256"]
    return await api.post(f"{BASE}/firma", json=_cuerpo(huella, **cambios), headers=cabecera)


# --- enviar ----------------------------------------------------------------------------------------------------------

async def test_enviar_encola_el_correo_sin_el_token_y_queda_auditado(escenario, fabrica):
    api, susc, _ = escenario

    cuerpo = await _enviar(api, susc)

    assert cuerpo["estado"] == "ACTIVO" and "token" not in str(cuerpo)
    (enlace,) = _enlaces(fabrica)
    (evento,) = _eventos(fabrica)
    assert evento["payload"]["enlace_id"] == enlace.id
    assert set(evento["payload"]) == {"enlace_id", "suscripcion_id", "cliente_id", "numero_poliza", "producto", "vence_en"}
    token = await _token(api, enlace.id)
    assert token not in str(evento)
    fila = _auditoria(fabrica)[-1]
    assert (fila.accion, fila.resultado, fila.asesor_id) == ("ENVIAR_ENLACE_FIRMA", "EXITO", ASESOR_ID)


async def test_solo_el_asesor_de_la_cotizacion_envia_y_el_rechazo_queda_auditado(escenario, fabrica):
    api, susc, _ = escenario

    assert (await api.post(_ruta(susc, "/firma/enlace"))).status_code == 401
    r = await api.post(_ruta(susc, "/firma/enlace"), headers=OTRO_ASESOR)

    assert r.status_code == 403 and _enlaces(fabrica) == []
    assert (_auditoria(fabrica)[-1].accion, _auditoria(fabrica)[-1].resultado) == ("ENVIAR_ENLACE_FIRMA", "ACCESO_DENEGADO")


async def test_enviar_enseguida_se_rechaza_y_pasada_la_espera_invalida_el_anterior(escenario, fabrica):
    api, susc, reloj = escenario
    await _enviar(api, susc)
    primero = {"X-Enlace-Firma": await _token(api, _enlaces(fabrica)[0].id)}

    r = await api.post(_ruta(susc, "/firma/enlace"), headers=ASESOR)
    assert r.status_code == 429 and r.json()["error"]["code"] == "CORREO_MUY_SEGUIDO" and len(_enlaces(fabrica)) == 1

    reloj.avanzar(61)
    await _enviar(api, susc)

    assert sorted(e.estado for e in _enlaces(fabrica)) == ["ACTIVO", "REEMPLAZADO"]
    r = await api.get(f"{BASE}/documento", headers=primero)
    assert r.status_code == 410 and r.json()["error"]["code"] == "ENLACE_REEMPLAZADO"


async def test_no_se_puede_enviar_fuera_del_paso_de_firma(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)

    r = await api.post(_ruta(susc, "/firma/enlace"), headers=ASESOR)

    assert r.status_code == 409 and r.json()["error"]["code"] == "FIRMA_NO_DISPONIBLE"


async def test_el_vencimiento_nunca_pasa_del_plazo_de_la_firma(escenario, fabrica):
    api, susc, reloj = escenario
    reloj.avanzar(20 * 3600)  # el cobro fue hace 21 h: quedan 3 h para firmar

    await _enviar(api, susc)

    (enlace,) = _enlaces(fabrica)
    limite = _saga(fabrica, susc.id).firma_vence_en
    assert enlace.vence_en == limite


async def test_el_detalle_muestra_el_enlace_sin_el_token(escenario, fabrica):
    api, susc, _ = escenario
    assert (await api.get(_ruta(susc), headers=ASESOR)).json()["enlace_firma"] is None

    await _enviar(api, susc)

    detalle = (await api.get(_ruta(susc), headers=ASESOR)).json()["enlace_firma"]
    assert detalle["estado"] == "ACTIVO" and set(detalle) == {"estado", "vence_en", "creado_en"}
    lista = (await api.get("/v1/suscripciones/asistidas", headers=ASESOR)).json()["items"]
    assert next(i for i in lista if i["id"] == susc.id)["enlace_firma"]["estado"] == "ACTIVO"


# --- token interno ---------------------------------------------------------------------------------------------------

async def test_el_token_interno_exige_la_clave_de_servicio(escenario, fabrica):
    api, susc, _ = escenario
    await _enviar(api, susc)
    ruta = f"/v1/interno/firma/enlaces/{_enlaces(fabrica)[0].id}/token"

    assert (await api.post(ruta, json={})).status_code == 401
    assert (await api.post(ruta, json={}, headers={"X-Service-Key": "otra"})).status_code == 401
    assert (await api.post(ruta, json={}, headers=ASESOR)).status_code == 401


async def test_la_ruta_lleva_el_idioma_y_el_token_en_el_fragmento(escenario, fabrica):
    api, susc, _ = escenario
    await _enviar(api, susc)

    r = await api.post(f"/v1/interno/firma/enlaces/{_enlaces(fabrica)[0].id}/token", json={"idioma": "en-US"}, headers=SERVICIO)

    assert r.json()["ruta"].startswith("/en-US/sign#t=")


async def test_un_reintento_del_correo_recibe_el_mismo_token(escenario, fabrica):
    api, susc, _ = escenario
    await _enviar(api, susc)
    enlace_id = _enlaces(fabrica)[0].id

    assert await _token(api, enlace_id) == await _token(api, enlace_id)


async def test_un_enlace_reemplazado_o_inexistente_no_entrega_token(escenario, fabrica):
    api, susc, reloj = escenario
    await _enviar(api, susc)
    viejo = _enlaces(fabrica)[0].id
    reloj.avanzar(61)
    await _enviar(api, susc)

    for enlace_id in (viejo, "no-existe"):
        r = await api.post(f"/v1/interno/firma/enlaces/{enlace_id}/token", json={}, headers=SERVICIO)
        assert r.status_code == 410


# --- el cliente firma --------------------------------------------------------------------------------------------------

async def test_el_cliente_revisa_pide_el_codigo_y_firma_sin_sesion(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)

    documento = await api.get(f"{BASE}/documento", headers=cabecera)
    assert documento.status_code == 200 and documento.json()["estado"] == "PENDIENTE"
    pdf = await api.get(f"{BASE}/documento/pdf", headers=cabecera)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["cache-control"] == "no-store"
    assert (await api.post(f"{BASE}/codigo", headers=cabecera)).status_code == 202

    r = await api.post(
        f"{BASE}/firma",
        json=_cuerpo(documento.json()["documento_sha256"]),
        headers={**cabecera, "X-Forwarded-For": "190.1.2.3", "User-Agent": "Navegador del cliente"},
    )

    assert r.status_code == 200 and r.json()["estado_saga"] == "EMITIDA" and r.json()["canal"] == "WEB"
    (firma,) = _firmas(fabrica)
    assert (firma.firmante_id, firma.modalidad, firma.asesor_id) == ("cli-1", "ASISTIDA_ENLACE", ASESOR_ID)
    assert (firma.ip, firma.dispositivo) == ("190.1.2.3", "Navegador del cliente")
    assert _poliza(fabrica, susc.id).estado == "VIGENTE"
    assert _enlaces(fabrica)[0].estado == "USADO" and _enlaces(fabrica)[0].usado_en is not None


async def test_firmar_por_enlace_dispara_la_misma_confirmacion(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)

    await _firmar(api, cabecera)

    tipos = [x.payload["event_type"] for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]
    assert tipos == ["firma_enlace_solicitada", "poliza_emitida"]


async def test_un_enlace_usado_no_abre_el_documento_y_repetir_devuelve_la_misma_firma(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)
    await _firmar(api, cabecera)

    documento = await api.get(f"{BASE}/documento", headers=cabecera)
    assert documento.json()["estado"] == "FIRMADA"
    assert (await api.get(f"{BASE}/documento/pdf", headers=cabecera)).status_code == 409
    assert (await api.post(f"{BASE}/codigo", headers=cabecera)).status_code == 409

    repetida = await api.post(f"{BASE}/firma", json=_cuerpo(documento.json()["documento_sha256"]), headers=cabecera)
    assert repetida.status_code == 200 and repetida.headers["idempotent-replay"] == "true"
    assert len(_firmas(fabrica)) == 1


async def test_si_el_asesor_ya_firmo_en_pantalla_el_enlace_lo_dice_y_conserva_esa_evidencia(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)
    await api.post(_ruta(susc, "/firma"), json=_cuerpo(await _huella(api, susc)), headers=ASESOR)

    r = await api.get(f"{BASE}/documento", headers=cabecera)

    assert r.json()["estado"] == "FIRMADA" and _firmas(fabrica)[0].modalidad == "ASISTIDA_PANTALLA"


@pytest.mark.parametrize("cabecera", [{}, {"X-Enlace-Firma": "x"}, {"X-Enlace-Firma": "aaa.bbb"}])
async def test_sin_token_o_con_uno_alterado_es_401(escenario, cabecera):
    api, *_ = escenario

    r = await api.get(f"{BASE}/documento", headers=cabecera)

    assert r.status_code == 401 and r.json()["error"]["code"] == "ENLACE_INVALIDO"


async def test_un_enlace_vencido_es_410(escenario, fabrica):
    api, susc, reloj = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)

    reloj.avanzar(24 * 3600)
    r = await api.get(f"{BASE}/documento", headers=cabecera)

    assert r.status_code == 410 and r.json()["error"]["code"] == "ENLACE_VENCIDO"


async def test_un_token_del_certificado_no_sirve_para_firmar(escenario, fabrica):
    """Aunque lo firme la misma clave de origen: la de firma se deriva con otro propósito."""
    api, susc, reloj = escenario
    await _enviar(api, susc)
    enlace_id = _enlaces(fabrica)[0].id
    vence = reloj() + timedelta(hours=1)
    # En producción `CLAVE_ENLACES` es la derivada; la de certificados es otra.
    certificado = FirmadorEnlaces("clave-de-los-certificados-en-origen", timedelta(hours=1)).firmar(enlace_id, "es-CO", vence)

    r = await api.get(f"{BASE}/documento", headers={"X-Enlace-Firma": certificado})

    assert r.status_code == 401


def test_la_clave_de_firma_es_distinta_de_la_de_certificados(monkeypatch):
    from app.suscripcion.config import ConfigSuscripcion
    from app.suscripcion.wiring import construir_servicio_enlace_firma

    origen = "clave-de-los-certificados-en-origen"
    config = ConfigSuscripcion.desde_entorno()
    object.__setattr__(config, "certificados_clave_enlaces", origen)

    servicio = construir_servicio_enlace_firma(config, firma=None, engine=None)

    esperada = hmac.new(origen.encode(), b"enlace-firma", hashlib.sha256).hexdigest().encode()
    assert servicio._firmador._clave == esperada != origen.encode()


async def test_un_codigo_erroneo_cuenta_y_al_agotarse_se_devuelve_el_cobro(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)

    primero = await _firmar(api, cabecera, codigo="000000")
    assert primero.status_code == 403 and primero.json()["error"]["code"] == "OTP_INVALIDO"
    await _firmar(api, cabecera, codigo="000000")
    huella = _poliza(fabrica, susc.id).documento_sha256
    ultimo = await api.post(f"{BASE}/firma", json=_cuerpo(huella, codigo="000000"), headers=cabecera)

    assert ultimo.status_code == 409 and ultimo.json()["error"]["code"] == "FIRMA_FALLIDA"
    assert _saga(fabrica, susc.id).compensacion_estado == "SOLICITADA"
    assert _enlaces(fabrica)[0].estado == "ACTIVO"  # no se firmó con él


async def test_un_hash_alterado_se_rechaza_y_el_enlace_sigue_activo(escenario, fabrica):
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)

    r = await api.post(f"{BASE}/firma", json=_cuerpo("b" * 64), headers=cabecera)

    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENTO_ALTERADO"
    assert _enlaces(fabrica)[0].estado == "ACTIVO"


async def test_el_token_y_el_codigo_no_quedan_en_los_logs_ni_en_la_auditoria(escenario, fabrica, caplog):
    caplog.set_level(logging.DEBUG)
    api, susc, _ = escenario
    cabecera = await _enlace_listo(api, susc, fabrica)
    token = cabecera["X-Enlace-Firma"]

    await _firmar(api, cabecera, codigo="123456")

    assert token not in caplog.text and "123456" not in caplog.text
    assert all(token not in str(vars(f)) for f in _auditoria(fabrica))


async def test_las_rutas_de_movil_y_de_la_consola_no_cambian(escenario):
    api, susc, _ = escenario

    assert (await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers={"Authorization": "Bearer token-1"})).status_code == 200
    assert (await api.get(_ruta(susc, "/documento-firma"), headers=ASESOR)).status_code == 200


async def test_el_evento_tiene_la_forma_del_ejemplo(escenario, fabrica):
    api, susc, _ = escenario
    ejemplo = json.loads((Path(__file__).parent / "contratos" / "firma_enlace_solicitada.json").read_text(encoding="utf-8"))

    await _enviar(api, susc)

    (evento,) = _eventos(fabrica)
    assert evento["event_type"] == ejemplo["event_type"] and set(evento["payload"]) == set(ejemplo["payload"])
