"""HU39 por HTTP: el asesor ve el certificado de sus clientes y pide reenviar la confirmación por correo."""

import json
from pathlib import Path

import jsonschema
import pytest
from app.suscripcion.api_certificado import router as certificado
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.polizas import EVENTO_REENVIO_CONFIRMACION
from httpx import ASGITransport, AsyncClient

from tests.test_api_asistida import (
    ASESOR,
    OTRO_ASESOR,
    _armar_app,
    _asignar,
    _auditoria,
    _cuerpo,
    _huella,
    _jwt,
    _ruta,
)
from tests.test_contrato_poliza_emitida import ESQUEMA_PAYLOAD
from tests.test_firma import _lista, _texto
from tests.test_polizas import _poliza
from tests.test_servicio import _salidas

CONTRATOS = Path(__file__).parent / "contratos"


@pytest.fixture
async def escenario(armar, fabrica):
    """Una saga lista para firmar, de una cotización del asesor; la API también expone la ruta del titular."""
    servicio_firma, susc, _almacen, reloj = await _lista(armar, fabrica)
    _asignar(fabrica, susc.id)
    app = _armar_app(armar(), servicio_firma, fabrica)
    app.include_router(certificado)
    app.state.servicio_certificado = app.state.servicio_asistida._certificados
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, reloj


async def _emitir(api, susc):
    r = await api.post(_ruta(susc, "/firma"), json=_cuerpo(await _huella(api, susc)), headers=ASESOR)
    assert r.status_code == 200 and r.json()["estado_saga"] == "EMITIDA"


def _eventos(fabrica):
    return [x.payload for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]


def _ultima_accion(fabrica):
    fila = _auditoria(fabrica)[-1]
    return fila.accion, fila.resultado


# --- la firma asistida dispara la confirmación --------------------------------------------------------------------------

async def test_firmar_en_la_consola_publica_el_evento_del_que_sale_el_correo(escenario, fabrica):
    """Notificaciones no distingue canales: basta con que el evento tenga la forma del contrato."""
    api, susc, _ = escenario

    await _emitir(api, susc)

    (evento,) = _eventos(fabrica)
    assert evento["event_type"] == "poliza_emitida"
    jsonschema.validate(evento["payload"], ESQUEMA_PAYLOAD)
    assert (evento["payload"]["suscripcion_id"], evento["payload"]["cliente_id"]) == (susc.id, "cli-1")


# --- certificado -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("metodo", "cola"), [("get", "/certificado"), ("post", "/confirmacion/reenvio")])
async def test_cada_ruta_exige_el_asesor_de_la_cotizacion(escenario, fabrica, metodo, cola):
    api, susc, _ = escenario
    await _emitir(api, susc)
    llamar = getattr(api, metodo)

    assert (await llamar(_ruta(susc, cola))).status_code == 401
    # El token del cliente no sirve aquí, y un 401 no es una acción del asesor.
    assert (await llamar(_ruta(susc, cola), headers={"Authorization": "Bearer token-1"})).status_code == 401
    assert (await llamar(_ruta(susc, cola), headers=_jwt(rol="OPERADOR"))).status_code == 403
    r = await llamar(_ruta(susc, cola), headers=OTRO_ASESOR)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCESO_DENEGADO"
    assert _ultima_accion(fabrica)[1] == "ACCESO_DENEGADO"


async def test_el_asesor_descarga_el_certificado_y_queda_auditado(escenario, fabrica):
    api, susc, _ = escenario
    await _emitir(api, susc)
    numero = _poliza(fabrica, susc.id).numero_poliza

    r = await api.get(_ruta(susc, "/certificado"), headers=ASESOR)

    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="certificado-{numero}.pdf"'
    assert r.headers["cache-control"] == "no-store"
    assert "Certificado de cobertura" in _texto(r.content)
    assert _ultima_accion(fabrica) == ("DESCARGAR_CERTIFICADO", "EXITO")


async def test_el_certificado_sale_en_el_idioma_de_la_consola(escenario):
    api, susc, _ = escenario
    await _emitir(api, susc)

    r = await api.get(_ruta(susc, "/certificado"), headers={**ASESOR, "Accept-Language": "en-US"})

    assert "Certificate of coverage" in _texto(r.content)


async def test_sin_emitir_no_hay_certificado_y_el_intento_queda_auditado(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.get(_ruta(susc, "/certificado"), headers=ASESOR)

    assert r.status_code == 409 and r.json()["error"]["code"] == "CERTIFICADO_NO_DISPONIBLE"
    assert _ultima_accion(fabrica) == ("DESCARGAR_CERTIFICADO", "CERTIFICADO_NO_DISPONIBLE")


async def test_una_suscripcion_inexistente_es_404(escenario):
    api, *_ = escenario

    r = await api.get("/v1/suscripciones/asistidas/no-existe/certificado", headers=ASESOR)

    assert r.status_code == 404


async def test_la_ruta_del_titular_no_cambia(escenario):
    api, susc, _ = escenario
    await _emitir(api, susc)

    titular = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers={"Authorization": "Bearer token-1"})
    asesor = await api.get(_ruta(susc, "/certificado"), headers=ASESOR)
    sin_sesion = await api.get(f"/v1/suscripciones/{susc.id}/certificado", headers=ASESOR)

    assert titular.status_code == 200 and titular.content == asesor.content
    assert sin_sesion.status_code == 401  # el JWT del asesor no abre la ruta del titular


# --- reenvío de la confirmación -------------------------------------------------------------------------------------

async def test_reenviar_encola_un_evento_propio_con_los_datos_de_la_emision(escenario, fabrica):
    api, susc, _ = escenario
    await _emitir(api, susc)

    r = await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)

    assert r.status_code == 202 and r.content == b""
    emitida, reenvio = _eventos(fabrica)
    assert reenvio["event_type"] == EVENTO_REENVIO_CONFIRMACION
    assert reenvio["event_id"] != emitida["event_id"]
    assert reenvio["payload"] == emitida["payload"]
    assert _ultima_accion(fabrica) == ("REENVIAR_CONFIRMACION", "EXITO")


async def test_el_evento_de_reenvio_tiene_la_forma_del_ejemplo(escenario, fabrica):
    api, susc, _ = escenario
    await _emitir(api, susc)
    await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)
    ejemplo = json.loads((CONTRATOS / "confirmacion_reenvio_solicitada.json").read_text(encoding="utf-8"))

    publicado = _eventos(fabrica)[-1]

    assert publicado["event_type"] == ejemplo["event_type"]
    assert set(publicado) == set(ejemplo) | ({"trace_id"} & set(publicado))
    jsonschema.validate(ejemplo["payload"], ESQUEMA_PAYLOAD)
    jsonschema.validate(publicado["payload"], ESQUEMA_PAYLOAD)


async def test_reenviar_enseguida_se_rechaza_y_pasada_la_espera_se_acepta(escenario, fabrica):
    api, susc, reloj = escenario
    await _emitir(api, susc)
    assert (await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)).status_code == 202

    reloj.avanzar(59)
    r = await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)
    assert r.status_code == 429 and r.json()["error"]["code"] == "CORREO_MUY_SEGUIDO"
    assert _ultima_accion(fabrica) == ("REENVIAR_CONFIRMACION", "CORREO_MUY_SEGUIDO")
    assert len(_eventos(fabrica)) == 2

    reloj.avanzar(2)
    assert (await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)).status_code == 202
    assert len(_eventos(fabrica)) == 3


async def test_sin_emitir_no_se_puede_reenviar(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.post(_ruta(susc, "/confirmacion/reenvio"), headers=ASESOR)

    assert r.status_code == 409 and r.json()["error"]["code"] == "CERTIFICADO_NO_DISPONIBLE"
    assert _eventos(fabrica) == []
