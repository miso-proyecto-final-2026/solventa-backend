"""HU33 por HTTP: MS Socios inicia la saga a nombre de un cliente, la consulta y gestiona la firma desde la app del socio."""

import logging

import pytest
from app.suscripcion.api_socios import router as socios
from app.suscripcion.errores import registrar_errores
from app.suscripcion.firma_socio import ServicioFirmaSocio
from app.suscripcion.modelos_db import AuditoriaSocioDB, SuscripcionDB
from app.suscripcion.servicio import clave_de_socio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.dobles import CotizacionesFalsas, cotizacion
from tests.test_firma import OTP, _firmas, _lista, _saga
from tests.test_polizas import _poliza

CLAVE_SERVICIO = "clave-de-servicio-de-prueba"
SOCIO = "socio-a"
OTRO_SOCIO = "socio-b"
TOKEN_PAGO = "tok_sim_abc123"
BASE = "/v1/interno/socios/suscripciones"
CABECERAS = {"X-Service-Key": CLAVE_SERVICIO}


@pytest.fixture(autouse=True)
def _claves_de_servicio(monkeypatch):
    monkeypatch.setenv("SUSCRIPCION_SERVICE_KEYS", f"{CLAVE_SERVICIO},otra-clave")


def _armar_app(servicio_suscripcion, servicio_firma, fabrica):
    app = FastAPI()
    app.include_router(socios)
    registrar_errores(app)
    app.state.servicio_suscripcion = servicio_suscripcion
    app.state.servicio_socio = ServicioFirmaSocio(
        fabrica=fabrica, suscripciones=servicio_suscripcion, firma=servicio_firma, reloj=servicio_firma._reloj
    )
    app.state.tareas = set()
    return app


def _asignar(fabrica, susc_id, socio=SOCIO):
    with fabrica() as s, s.begin():
        s.get(SuscripcionDB, susc_id).socio_client_id = socio


def _auditoria(fabrica) -> list[AuditoriaSocioDB]:
    with fabrica() as s:
        return list(s.execute(select(AuditoriaSocioDB).order_by(AuditoriaSocioDB.id)).scalars())


@pytest.fixture
async def escenario(armar, fabrica):
    """Una saga lista para firmar, iniciada por `socio-a` para el cliente `cli-1`."""
    servicio_firma, susc, _almacen, reloj = await _lista(armar, fabrica)
    _asignar(fabrica, susc.id)
    app = _armar_app(armar(), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, reloj


@pytest.fixture
async def inicio(armar, fabrica):
    """API para iniciar sagas: `cot-2` y `cot-4` son de `cli-2`, `cot-3` de `cli-3`."""
    servicio_firma, _susc, *_ = await _lista(armar, fabrica)
    cotizaciones = CotizacionesFalsas(
        cotizacion(id="cot-2", cliente="cli-2"), cotizacion(id="cot-3", cliente="cli-3"), cotizacion(id="cot-4", cliente="cli-2")
    )
    app = _armar_app(armar(cotizaciones=cotizaciones), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _inicio(cotizacion_id="cot-2", cliente_id="cli-2", socio=SOCIO, **cambios):
    return {
        "socio_client_id": socio,
        "cliente_id": cliente_id,
        "cotizacion_id": cotizacion_id,
        "metodo_pago": TOKEN_PAGO,
        "acepta_terminos": True,
        **cambios,
    }


def _con_clave(clave="k1"):
    return {**CABECERAS, "Idempotency-Key": clave}


def _ruta(susc, cola=""):
    return f"{BASE}/{susc.id}{cola}"


def _consulta(socio=SOCIO):
    return {"params": {"socio_client_id": socio}, "headers": CABECERAS}


async def _huella(api, susc):
    return (await api.get(_ruta(susc, "/documento-firma"), **_consulta())).json()["documento_sha256"]


def _firma(huella, **cambios):
    return {"socio_client_id": SOCIO, "documento_sha256": huella, "documento_revisado": True, "codigo": OTP, "canal": "WEB", **cambios}


# --- iniciar y atribución (criterios 1, 2 y 7) --------------------------------------------------------------------------


async def test_el_socio_inicia_la_saga_a_nombre_del_cliente_y_queda_atribuida(inicio, fabrica):
    r = await inicio.post(BASE, json=_inicio(), headers=_con_clave())

    assert r.status_code == 202 and r.json()["estado_saga"] == "EN_PROGRESO"
    saga = _saga(fabrica, r.json()["id"])
    assert (saga.cliente_id, saga.socio_client_id, saga.asesor_id) == ("cli-2", SOCIO, None)
    ultima = _auditoria(fabrica)[-1]
    assert (ultima.accion, ultima.resultado, ultima.suscripcion_id, ultima.socio_client_id) == (
        "INICIAR_SUSCRIPCION", "EXITO", saga.id, SOCIO,
    )


async def test_la_clave_de_idempotencia_de_la_saga_queda_acotada_al_socio(inicio, fabrica):
    r = await inicio.post(BASE, json=_inicio(), headers=_con_clave("k1"))

    saga = _saga(fabrica, r.json()["id"])
    assert saga.clave_idempotencia == clave_de_socio(SOCIO, "k1")
    assert saga.clave_idempotencia != clave_de_socio(OTRO_SOCIO, "k1") and len(saga.clave_idempotencia) <= 100


async def test_repetir_con_la_misma_clave_devuelve_la_misma_saga(inicio):
    primera = await inicio.post(BASE, json=_inicio(), headers=_con_clave())

    segunda = await inicio.post(BASE, json=_inicio(), headers=_con_clave())

    assert segunda.status_code == 202 and segunda.headers["idempotent-replay"] == "true"
    assert segunda.json()["id"] == primera.json()["id"]


async def test_la_misma_clave_con_otra_cotizacion_es_409(inicio):
    await inicio.post(BASE, json=_inicio(), headers=_con_clave())

    r = await inicio.post(BASE, json=_inicio("cot-4"), headers=_con_clave())

    assert r.status_code == 409 and r.json()["error"]["code"] == "CLAVE_IDEMPOTENCIA_REUTILIZADA"


async def test_otro_socio_con_la_misma_clave_no_recibe_la_saga_del_primero(inicio):
    primera = await inicio.post(BASE, json=_inicio(), headers=_con_clave("k1"))

    # Otro socio con la misma clave sobre otra cotización: es otra venta, no una repetición.
    otra = await inicio.post(BASE, json=_inicio("cot-3", "cli-3", socio=OTRO_SOCIO), headers=_con_clave("k1"))

    assert otra.status_code == 202 and "idempotent-replay" not in otra.headers
    assert otra.json()["id"] != primera.json()["id"]


async def test_una_cotizacion_con_saga_en_curso_no_se_vende_dos_veces(inicio):
    await inicio.post(BASE, json=_inicio(), headers=_con_clave("k1"))

    r = await inicio.post(BASE, json=_inicio(socio=OTRO_SOCIO), headers=_con_clave("k2"))

    assert r.status_code == 409 and r.json()["error"]["code"] == "COTIZACION_YA_USADA"


@pytest.mark.parametrize(
    ("cuerpo", "status", "codigo"),
    [
        (_inicio("cot-3", "cli-2"), 404, "COTIZACION_NO_ENCONTRADA"),  # cotización de otro cliente: no se revela
        (_inicio("no-existe"), 404, "COTIZACION_NO_ENCONTRADA"),
        (_inicio(acepta_terminos=False), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="4242 4242 4242 4242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="4242424242424242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="tok_4242424242424242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="3782 822463 10005"), 422, "VALIDATION_ERROR"),
    ],
)
async def test_iniciar_rechazado_queda_auditado_sin_suscripcion(inicio, fabrica, cuerpo, status, codigo):
    r = await inicio.post(BASE, json=cuerpo, headers=_con_clave())

    assert r.status_code == status and r.json()["error"]["code"] == codigo
    ultima = _auditoria(fabrica)[-1]
    assert (ultima.accion, ultima.resultado, ultima.suscripcion_id) == ("INICIAR_SUSCRIPCION", codigo, None)
    assert ultima.cotizacion_id == cuerpo["cotizacion_id"]


async def test_sin_clave_de_idempotencia_es_422(inicio):
    r = await inicio.post(BASE, json=_inicio(), headers=CABECERAS)

    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_el_token_de_pago_no_queda_en_logs_ni_en_auditoria(inicio, fabrica, caplog):
    with caplog.at_level(logging.DEBUG):
        await inicio.post(BASE, json=_inicio(), headers=_con_clave())

    assert TOKEN_PAGO not in caplog.text
    fila = _auditoria(fabrica)[-1]
    assert TOKEN_PAGO not in f"{fila.accion} {fila.resultado} {fila.cotizacion_id}"


# --- acceso de servicio ---------------------------------------------------------------------------------------------


async def test_cada_ruta_exige_la_clave_de_servicio(escenario):
    api, susc, _ = escenario
    llamadas = [
        ("get", _ruta(susc), {"params": {"socio_client_id": SOCIO}}),
        ("get", _ruta(susc, "/documento-firma"), {"params": {"socio_client_id": SOCIO}}),
        ("get", _ruta(susc, "/documento-firma/pdf"), {"params": {"socio_client_id": SOCIO}}),
        ("post", _ruta(susc, "/firma/codigo"), {"json": {"socio_client_id": SOCIO}}),
        ("post", _ruta(susc, "/firma"), {"json": _firma("a" * 64)}),
        ("post", BASE, {"json": _inicio()}),
    ]
    for metodo, ruta, extra in llamadas:
        llamar = getattr(api, metodo)
        assert (await llamar(ruta, **extra)).status_code == 401
        assert (await llamar(ruta, headers={"X-Service-Key": "incorrecta"}, **extra)).status_code == 401
        # El token de un cliente final no sirve aquí.
        assert (await llamar(ruta, headers={"Authorization": "Bearer token-1"}, **extra)).status_code == 401


# --- aislamiento (criterio 10) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "cola"),
    [
        ("get", ""),
        ("get", "/documento-firma"),
        ("get", "/documento-firma/pdf"),
        ("post", "/firma/codigo"),
        ("post", "/firma"),
    ],
)
async def test_la_saga_de_otro_socio_responde_como_si_no_existiera(escenario, metodo, cola):
    api, susc, _ = escenario
    ajena = {"params": {"socio_client_id": OTRO_SOCIO}} if metodo == "get" else {"json": _firma("a" * 64, socio_client_id=OTRO_SOCIO)}
    if cola == "/firma/codigo":
        ajena = {"json": {"socio_client_id": OTRO_SOCIO}}
    inexistente_ruta = f"{BASE}/no-existe{cola}"

    r = await getattr(api, metodo)(_ruta(susc, cola), headers=CABECERAS, **ajena)
    n = await getattr(api, metodo)(inexistente_ruta, headers=CABECERAS, **ajena)

    assert r.status_code == 404 and r.json()["error"]["code"] == "SUSCRIPCION_NO_ENCONTRADA"
    assert (r.status_code, r.json()["error"]["code"]) == (n.status_code, n.json()["error"]["code"])


async def test_una_saga_que_no_es_de_ningun_socio_tampoco_se_ve(armar, fabrica):
    servicio_firma, susc, *_ = await _lista(armar, fabrica)  # sin `socio_client_id`: es del cliente o del asesor
    app = _armar_app(armar(), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        r = await api.get(_ruta(susc), **_consulta())

    assert r.status_code == 404


async def test_el_rechazo_por_otro_socio_queda_auditado_con_la_referencia(escenario, fabrica):
    api, susc, _ = escenario

    await api.post(_ruta(susc, "/firma/codigo"), json={"socio_client_id": OTRO_SOCIO}, headers=CABECERAS)

    ultima = _auditoria(fabrica)[-1]
    assert (ultima.socio_client_id, ultima.accion, ultima.resultado) == (OTRO_SOCIO, "SOLICITAR_CODIGO", "SUSCRIPCION_NO_ENCONTRADA")
    assert ultima.suscripcion_id is None  # la saga no es suya: no se referencia


# --- consultar (criterios 4 y 8) ----------------------------------------------------------------------------------------


async def test_el_detalle_trae_el_estado_de_la_saga_y_no_datos_internos(escenario):
    api, susc, _ = escenario

    r = await api.get(_ruta(susc), **_consulta())

    assert r.status_code == 200
    cuerpo = r.json()
    assert (cuerpo["id"], cuerpo["estado_saga"], cuerpo["paso_actual"]) == (susc.id, "COBRO_REALIZADO", "FIRMA")
    assert cuerpo["pasos"] == ["DECISION", "COBRO", "FIRMA", "EMISION"]
    assert cuerpo["firma"]["estado"] == "PENDIENTE" and cuerpo["poliza"]["estado"] == "PENDIENTE_FIRMA"
    for interno in ("cliente_id", "socio_client_id", "asesor_id", "metodo_pago", "documento_s3_key", "clave_idempotencia"):
        assert interno not in r.text


async def test_tras_la_firma_el_detalle_muestra_la_poliza_emitida_con_su_numero(escenario):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    assert (await api.post(_ruta(susc, "/firma"), json=_firma(huella), headers=CABECERAS)).status_code == 200

    cuerpo = (await api.get(_ruta(susc), **_consulta())).json()

    assert cuerpo["estado_saga"] == "EMITIDA" and cuerpo["firma"]["estado"] == "FIRMADA"
    poliza = cuerpo["poliza"]
    assert poliza["estado"] == "VIGENTE" and poliza["numero_poliza"] and poliza["documento_sha256"] == huella
    assert poliza["fecha_emision"] is not None and poliza["fecha_vencimiento"] > poliza["vigencia_desde"]


# --- firmar (criterio 6) ----------------------------------------------------------------------------------------------


async def test_el_documento_por_firmar_se_lee_y_se_descarga_desde_el_socio(escenario):
    api, susc, _ = escenario

    doc = await api.get(_ruta(susc, "/documento-firma"), **_consulta())
    pdf = await api.get(_ruta(susc, "/documento-firma/pdf"), **_consulta())

    assert doc.status_code == 200 and len(doc.json()["documento_sha256"]) == 64 and doc.json()["estado"] == "PENDIENTE"
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["cache-control"] == "no-store"


async def test_la_firma_del_socio_guarda_la_evidencia_del_cliente_y_el_socio_de_origen(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    assert (await api.post(_ruta(susc, "/firma/codigo"), json={"socio_client_id": SOCIO}, headers=CABECERAS)).status_code == 202

    r = await api.post(
        _ruta(susc, "/firma"),
        json=_firma(huella, ip="190.9.8.7", dispositivo="App del socio / Android"),
        headers=CABECERAS,
    )

    assert r.status_code == 200
    cuerpo = r.json()
    assert (cuerpo["estado_saga"], cuerpo["canal"], cuerpo["modalidad"], cuerpo["socio_client_id"]) == ("EMITIDA", "WEB", "SOCIO", SOCIO)
    firma = _firmas(fabrica)[0]
    assert (firma.firmante_id, firma.socio_client_id, firma.modalidad, firma.asesor_id) == ("cli-1", SOCIO, "SOCIO", None)
    assert (firma.ip, firma.dispositivo, firma.documento_sha256) == ("190.9.8.7", "App del socio / Android", huella)
    assert _poliza(fabrica, susc.id).estado == "VIGENTE"


async def test_una_ip_que_no_es_ip_se_descarta_de_la_evidencia(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.post(_ruta(susc, "/firma"), json=_firma(await _huella(api, susc), ip="no-es-una-ip"), headers=CABECERAS)

    assert r.status_code == 200 and _firmas(fabrica)[0].ip is None


async def test_firmar_dos_veces_devuelve_la_misma_firma(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)

    primera = await api.post(_ruta(susc, "/firma"), json=_firma(huella), headers=CABECERAS)
    segunda = await api.post(_ruta(susc, "/firma"), json=_firma(huella), headers=CABECERAS)

    assert segunda.status_code == 200 and segunda.headers["idempotent-replay"] == "true"
    assert segunda.json() == primera.json() and len(_firmas(fabrica)) == 1


async def test_un_codigo_erroneo_no_firma(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.post(_ruta(susc, "/firma"), json=_firma(await _huella(api, susc), codigo="000000"), headers=CABECERAS)

    assert r.status_code == 403 and r.json()["error"]["code"] == "OTP_INVALIDO" and _firmas(fabrica) == []


async def test_un_hash_alterado_se_rechaza(escenario):
    api, susc, _ = escenario

    r = await api.post(_ruta(susc, "/firma"), json=_firma("a" * 64), headers=CABECERAS)

    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENTO_ALTERADO"


async def test_cada_accion_queda_auditada_sin_el_codigo(escenario, fabrica, caplog):
    api, susc, _ = escenario
    huella = await _huella(api, susc)

    with caplog.at_level(logging.DEBUG):
        await api.get(_ruta(susc, "/documento-firma/pdf"), **_consulta())
        await api.post(_ruta(susc, "/firma/codigo"), json={"socio_client_id": SOCIO}, headers=CABECERAS)
        await api.post(_ruta(susc, "/firma"), json=_firma(huella, codigo="000000"), headers=CABECERAS)
        await api.post(_ruta(susc, "/firma"), json=_firma(huella), headers=CABECERAS)

    acciones = [(a.accion, a.resultado, a.suscripcion_id, a.socio_client_id) for a in _auditoria(fabrica)]
    assert acciones == [
        ("DESCARGAR_DOCUMENTO", "EXITO", susc.id, SOCIO),
        ("SOLICITAR_CODIGO", "EXITO", susc.id, SOCIO),
        ("FIRMAR", "OTP_INVALIDO", susc.id, SOCIO),
        ("FIRMAR", "EXITO", susc.id, SOCIO),
    ]
    assert OTP not in caplog.text and "000000" not in caplog.text


async def test_la_auditoria_del_socio_es_inmutable(escenario, fabrica):
    api, susc, _ = escenario
    await api.post(_ruta(susc, "/firma/codigo"), json={"socio_client_id": SOCIO}, headers=CABECERAS)

    with fabrica() as s, pytest.raises(RuntimeError, match="inmutable"), s.begin():
        s.execute(select(AuditoriaSocioDB)).scalars().first().resultado = "OTRO"
