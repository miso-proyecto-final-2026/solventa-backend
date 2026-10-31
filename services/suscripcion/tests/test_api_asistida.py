"""HU39 por HTTP: el asesor inicia la saga de su cotización, la sigue y gestiona la firma con el cliente presente."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.suscripcion.acceso import VerificadorJwtHs256
from app.suscripcion.api import router as suscripciones
from app.suscripcion.api_asistida import router as asistidas
from app.suscripcion.api_firma import router as firma
from app.suscripcion.errores import DependenciaNoDisponible, registrar_errores
from app.suscripcion.firma_asistida import ServicioFirmaAsistida
from app.suscripcion.modelos_db import AuditoriaAsesorDB, SuscripcionDB
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.dobles import CotizacionesFalsas, IdentidadFalsa, cotizacion
from tests.test_firma import OTP, _firmas, _lista, _saga, _texto
from tests.test_polizas import _poliza

SECRETO = "secreto-de-prueba-de-al-menos-32-bytes"
EMISOR = "solventa-identidad"
ASESOR_ID = "ase-1"
TOKEN_TARJETA = "tok_sim_abc123"


def _jwt(sub=ASESOR_ID, rol="ASESOR", secreto=SECRETO, minutos=30):
    exp = datetime.now(UTC) + timedelta(minutes=minutos)
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub, "rol": rol, "iss": EMISOR, "exp": exp}, secreto, "HS256")}


ASESOR = _jwt()
OTRO_ASESOR = _jwt(sub="ase-2")


def _armar_app(servicio_suscripcion, servicio_firma, fabrica, identidad=None):
    app = FastAPI()
    app.include_router(asistidas)
    app.include_router(suscripciones)
    app.include_router(firma)
    registrar_errores(app)
    app.state.servicio_suscripcion = servicio_suscripcion
    app.state.servicio_firma = servicio_firma
    app.state.servicio_asistida = ServicioFirmaAsistida(
        fabrica=fabrica,
        suscripciones=servicio_suscripcion,
        firma=servicio_firma,
        identidad=identidad or IdentidadFalsa(),
        reloj=servicio_firma._reloj,
    )
    app.state.verificador_asesor = VerificadorJwtHs256(SECRETO, EMISOR)
    app.state.tareas = set()
    return app


def _asignar(fabrica, susc_id, asesor_id=ASESOR_ID):
    with fabrica() as s, s.begin():
        s.get(SuscripcionDB, susc_id).asesor_id = asesor_id


def _auditoria(fabrica) -> list[AuditoriaAsesorDB]:
    with fabrica() as s:
        return list(s.execute(select(AuditoriaAsesorDB).order_by(AuditoriaAsesorDB.id)).scalars())


@pytest.fixture
async def escenario(armar, fabrica):
    """Una saga lista para firmar, de una cotización del asesor."""
    servicio_firma, susc, _almacen, reloj = await _lista(armar, fabrica)
    _asignar(fabrica, susc.id)
    app = _armar_app(armar(), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, susc, reloj


@pytest.fixture
async def inicio(armar, fabrica):
    """API para iniciar sagas: `cot-2` es del asesor, `cot-ajena` de otro y `cot-sc` de una persona sin cuenta."""
    servicio_firma, _susc, *_ = await _lista(armar, fabrica)
    cotizaciones = CotizacionesFalsas(
        replace(cotizacion(id="cot-2", cliente="cli-2"), asesor_id=ASESOR_ID),
        replace(cotizacion(id="cot-ajena", cliente="cli-2"), asesor_id="ase-2"),
        replace(cotizacion(id="cot-sc", cliente="pro-1"), asesor_id=ASESOR_ID, sin_cuenta=True),
    )
    app = _armar_app(armar(cotizaciones=cotizaciones), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _ruta(susc, cola=""):
    return f"/v1/suscripciones/asistidas/{susc.id}{cola}"


async def _huella(api, susc):
    return (await api.get(_ruta(susc, "/documento-firma"), headers=ASESOR)).json()["documento_sha256"]


def _cuerpo(huella, **cambios):
    return {"documento_sha256": huella, "documento_revisado": True, "codigo": OTP, **cambios}


def _inicio(cotizacion_id="cot-2", **cambios):
    return {"cotizacion_id": cotizacion_id, "metodo_pago": TOKEN_TARJETA, "acepta_terminos": True, **cambios}


# --- iniciar -------------------------------------------------------------------------------------------------------


async def test_el_asesor_inicia_la_saga_a_nombre_del_titular_de_su_cotizacion(inicio, fabrica):
    r = await inicio.post("/v1/suscripciones/asistidas", json=_inicio(), headers={**ASESOR, "Idempotency-Key": "k1"})

    assert r.status_code == 202 and r.json()["estado_saga"] == "EN_PROGRESO"
    saga = _saga(fabrica, r.json()["id"])
    assert (saga.cliente_id, saga.asesor_id) == ("cli-2", ASESOR_ID)
    ultima = _auditoria(fabrica)[-1]
    assert (ultima.accion, ultima.resultado, ultima.suscripcion_id, ultima.asesor_id) == (
        "INICIAR_SUSCRIPCION", "EXITO", saga.id, ASESOR_ID,
    )


async def test_repetir_con_la_misma_clave_devuelve_la_misma_saga(inicio):
    cabeceras = {**ASESOR, "Idempotency-Key": "k1"}
    primera = await inicio.post("/v1/suscripciones/asistidas", json=_inicio(), headers=cabeceras)

    segunda = await inicio.post("/v1/suscripciones/asistidas", json=_inicio(), headers=cabeceras)

    assert segunda.status_code == 202 and segunda.headers["idempotent-replay"] == "true"
    assert segunda.json()["id"] == primera.json()["id"]


@pytest.mark.parametrize(
    ("cuerpo", "status", "codigo"),
    [
        (_inicio("cot-ajena"), 403, "ACCESO_DENEGADO"),
        (_inicio("cot-sc"), 409, "COTIZACION_SIN_CUENTA"),
        (_inicio("no-existe"), 404, "COTIZACION_NO_ENCONTRADA"),
        (_inicio(acepta_terminos=False), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="4242 4242 4242 4242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="4242424242424242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="tok_4242424242424242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="tarjeta 4242-4242-4242-4242"), 422, "VALIDATION_ERROR"),
        (_inicio(metodo_pago="3782 822463 10005"), 422, "VALIDATION_ERROR"),
    ],
)
async def test_iniciar_rechazado_queda_auditado_sin_suscripcion(inicio, fabrica, cuerpo, status, codigo):
    r = await inicio.post("/v1/suscripciones/asistidas", json=cuerpo, headers={**ASESOR, "Idempotency-Key": "k1"})

    assert r.status_code == status and r.json()["error"]["code"] == codigo
    ultima = _auditoria(fabrica)[-1]
    assert (ultima.accion, ultima.resultado, ultima.suscripcion_id) == ("INICIAR_SUSCRIPCION", codigo, None)
    assert ultima.cotizacion_id == cuerpo["cotizacion_id"]


@pytest.mark.parametrize(
    "token",
    # El primero tiene un segmento de 12 dígitos que, sumado a sus vecinos, engañaba a la regla amplia de Pagos (backend#55).
    ["tok_sim_12345678-1234-4123-8123-123456789012", "tok_sim_rechazar_0f8fad5b-d9cb-469f-a165-70867728950e"],
)
async def test_un_token_con_uuid_no_se_confunde_con_una_tarjeta(inicio, token):
    r = await inicio.post(
        "/v1/suscripciones/asistidas", json=_inicio(metodo_pago=token), headers={**ASESOR, "Idempotency-Key": "k1"}
    )

    assert r.status_code == 202


async def test_el_token_de_pago_no_queda_en_logs_ni_en_auditoria(inicio, fabrica, caplog):
    with caplog.at_level(logging.DEBUG):
        await inicio.post("/v1/suscripciones/asistidas", json=_inicio(), headers={**ASESOR, "Idempotency-Key": "k1"})

    assert TOKEN_TARJETA not in caplog.text
    fila = _auditoria(fabrica)[-1]
    assert TOKEN_TARJETA not in f"{fila.accion} {fila.resultado} {fila.cotizacion_id}"


# --- acceso --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "cola"),
    [
        ("get", ""),
        ("get", "/documento-firma"),
        ("get", "/documento-firma/pdf"),
        ("post", "/firma/codigo"),
        ("get", "/firma/documento"),
    ],
)
async def test_cada_ruta_exige_el_asesor_de_la_cotizacion(escenario, metodo, cola):
    api, susc, _ = escenario
    llamar = getattr(api, metodo)

    assert (await llamar(_ruta(susc, cola))).status_code == 401
    assert (await llamar(_ruta(susc, cola), headers=_jwt(secreto="otro-secreto-distinto-de-al-menos-32b"))).status_code == 401
    assert (await llamar(_ruta(susc, cola), headers=_jwt(minutos=-1))).status_code == 401
    # El token del cliente no sirve aquí.
    assert (await llamar(_ruta(susc, cola), headers={"Authorization": "Bearer token-1"})).status_code == 401
    assert (await llamar(_ruta(susc, cola), headers=_jwt(rol="OPERADOR"))).status_code == 403
    sin_relacion = await llamar(_ruta(susc, cola), headers=OTRO_ASESOR)
    assert sin_relacion.status_code == 403 and sin_relacion.json()["error"]["code"] == "ACCESO_DENEGADO"


async def test_una_suscripcion_inexistente_es_404_y_el_intento_queda_sin_referencia(escenario, fabrica):
    api, *_ = escenario

    r = await api.post("/v1/suscripciones/asistidas/no-existe/firma/codigo", headers=ASESOR)

    assert r.status_code == 404
    ultima = _auditoria(fabrica)[-1]
    assert (ultima.resultado, ultima.suscripcion_id) == ("SUSCRIPCION_NO_ENCONTRADA", None)


async def test_el_rechazo_por_falta_de_relacion_queda_auditado(escenario, fabrica):
    api, susc, _ = escenario

    await api.post(_ruta(susc, "/firma"), json=_cuerpo("a" * 64), headers=OTRO_ASESOR)

    ultima = _auditoria(fabrica)[-1]
    assert (ultima.asesor_id, ultima.accion, ultima.resultado, ultima.suscripcion_id) == ("ase-2", "FIRMAR", "ACCESO_DENEGADO", susc.id)


# --- lista y detalle -----------------------------------------------------------------------------------------------


async def test_la_lista_solo_trae_las_sagas_del_asesor_con_su_progreso(escenario):
    api, susc, _ = escenario

    mia = await api.get("/v1/suscripciones/asistidas", headers=ASESOR)
    ajena = await api.get("/v1/suscripciones/asistidas", headers=OTRO_ASESOR)

    assert ajena.json()["items"] == [] and ajena.json()["total"] == 0
    assert mia.status_code == 200 and mia.json()["total"] == 1
    fila = mia.json()["items"][0]
    assert fila["id"] == susc.id and fila["estado_saga"] == "COBRO_REALIZADO" and fila["paso_actual"] == "FIRMA"
    assert fila["pasos"] == ["DECISION", "COBRO", "FIRMA", "EMISION"]
    assert fila["puede_gestionar_firma"] is True and fila["compensacion"] is None


async def test_la_lista_filtra_por_estado_y_valida_los_parametros(escenario):
    api, *_ = escenario

    assert (await api.get("/v1/suscripciones/asistidas?estado=COBRO_REALIZADO", headers=ASESOR)).json()["total"] == 1
    assert (await api.get("/v1/suscripciones/asistidas?estado=EMITIDA", headers=ASESOR)).json()["total"] == 0
    assert (await api.get("/v1/suscripciones/asistidas?estado=INVENTADO", headers=ASESOR)).status_code == 422
    assert (await api.get("/v1/suscripciones/asistidas?tamano=51", headers=ASESOR)).status_code == 422


async def test_el_cliente_se_identifica_con_lo_minimo(escenario):
    api, susc, _ = escenario

    cliente = (await api.get(_ruta(susc), headers=ASESOR)).json()["cliente"]

    assert cliente == {"nombre": "Ana María", "inicial_apellido": "P", "tipo_documento": "CC", "documento_enmascarado": "******300"}


async def test_si_identidad_no_responde_la_saga_se_muestra_sin_cliente(armar, fabrica):
    class IdentidadCaida(IdentidadFalsa):
        async def resumen_cliente(self, cliente_id):
            raise DependenciaNoDisponible

    servicio_firma, susc, *_ = await _lista(armar, fabrica)
    _asignar(fabrica, susc.id)
    app = _armar_app(armar(), servicio_firma, fabrica, identidad=IdentidadCaida())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        r = await api.get("/v1/suscripciones/asistidas", headers=ASESOR)

    assert r.status_code == 200 and r.json()["items"][0]["cliente"] is None


# --- estados permitidos --------------------------------------------------------------------------------------------


async def test_tras_agotar_los_codigos_no_se_puede_gestionar_aunque_siga_en_cobro_realizado(escenario):
    api, susc, _ = escenario
    huella = await _huella(api, susc)

    for _ in range(2):
        r = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella, codigo="000000"), headers=ASESOR)
        assert r.status_code == 403 and r.json()["error"]["code"] == "OTP_INVALIDO"
    r = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella, codigo="000000"), headers=ASESOR)

    assert r.status_code == 409 and r.json()["error"]["code"] == "FIRMA_FALLIDA"
    detalle = (await api.get(_ruta(susc), headers=ASESOR)).json()
    assert detalle["estado_saga"] == "COBRO_REALIZADO"  # hasta que MS Pagos confirme el reembolso
    assert detalle["puede_gestionar_firma"] is False
    assert detalle["compensacion"]["estado"] == "SOLICITADA" and detalle["compensacion"]["motivo"] == "FIRMA_FALLIDA"


async def test_vencido_el_plazo_no_se_puede_gestionar_y_firmar_responde_vencida(escenario):
    api, susc, reloj = escenario
    huella = await _huella(api, susc)
    reloj.ahora += timedelta(hours=24)

    assert (await api.get(_ruta(susc), headers=ASESOR)).json()["puede_gestionar_firma"] is False
    r = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FIRMA_VENCIDA"


async def test_fuera_de_cobro_realizado_no_se_puede_gestionar(armar, fabrica):
    servicio_firma, susc, *_ = await _lista(armar, fabrica)
    with fabrica() as s, s.begin():
        fila = s.get(SuscripcionDB, susc.id)
        fila.asesor_id, fila.estado_saga, fila.paso_actual = ASESOR_ID, "APROBADA", "COBRO"
    app = _armar_app(armar(), servicio_firma, fabrica)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        detalle = await api.get(_ruta(susc), headers=ASESOR)
        codigo = await api.post(_ruta(susc, "/firma/codigo"), headers=ASESOR)

    assert detalle.json()["puede_gestionar_firma"] is False
    assert codigo.status_code == 409 and codigo.json()["error"]["code"] == "FIRMA_NO_DISPONIBLE"


# --- firmar --------------------------------------------------------------------------------------------------------


async def test_la_firma_asistida_guarda_la_evidencia_del_cliente_y_la_del_asesor(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    assert (await api.post(_ruta(susc, "/firma/codigo"), headers=ASESOR)).status_code == 202

    r = await api.post(
        _ruta(susc, "/firma"),
        json=_cuerpo(huella),
        headers={**ASESOR, "X-Forwarded-For": "190.9.8.7", "User-Agent": "Chrome de la consola"},
    )

    assert r.status_code == 200
    assert (r.json()["estado_saga"], r.json()["canal"], r.json()["modalidad"], r.json()["asesor_id"]) == (
        "EMITIDA", "WEB", "ASISTIDA_PANTALLA", ASESOR_ID,
    )
    firma = _firmas(fabrica)[0]
    assert (firma.firmante_id, firma.asesor_id, firma.modalidad) == ("cli-1", ASESOR_ID, "ASISTIDA_PANTALLA")
    assert (firma.ip, firma.dispositivo, firma.documento_sha256) == ("190.9.8.7", "Chrome de la consola", huella)
    assert _poliza(fabrica, susc.id).estado == "VIGENTE"


async def test_la_ip_de_la_firma_asistida_es_la_que_pone_cloudfront_y_no_se_puede_falsificar(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.post(
        _ruta(susc, "/firma"),
        json=_cuerpo(await _huella(api, susc)),
        headers={**ASESOR, "CloudFront-Viewer-Address": "190.9.8.7:443", "X-Forwarded-For": "6.6.6.6"},
    )

    assert r.status_code == 200
    assert _firmas(fabrica)[0].ip == "190.9.8.7"


async def test_la_copia_firmada_dice_quien_asistio(escenario):
    api, susc, _ = escenario
    await api.post(_ruta(susc, "/firma"), json=_cuerpo(await _huella(api, susc)), headers=ASESOR)

    r = await api.get(_ruta(susc, "/firma/documento"), headers=ASESOR)

    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "firmada.pdf" in r.headers["content-disposition"]
    assert ASESOR_ID in _texto(r.content)


async def test_si_el_cliente_ya_firmo_no_se_sobrescribe_su_evidencia(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    await api.post(
        f"/v1/suscripciones/{susc.id}/firma",
        json={**_cuerpo(huella), "canal": "MOVIL"},
        headers={"Authorization": "Bearer token-1"},
    )

    r = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)

    assert r.status_code == 200 and r.headers["idempotent-replay"] == "true"
    assert (r.json()["canal"], r.json()["modalidad"], r.json()["asesor_id"]) == ("MOVIL", "CLIENTE", None)
    assert len(_firmas(fabrica)) == 1


async def test_firmar_dos_veces_no_crea_dos_firmas(escenario, fabrica):
    api, susc, _ = escenario
    huella = await _huella(api, susc)

    primera = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)
    segunda = await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)

    assert segunda.status_code == 200 and segunda.json() == primera.json() and len(_firmas(fabrica)) == 1


async def test_un_hash_alterado_se_rechaza_y_no_gasta_intentos(escenario, fabrica):
    api, susc, _ = escenario

    r = await api.post(_ruta(susc, "/firma"), json=_cuerpo("a" * 64), headers=ASESOR)

    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENTO_ALTERADO"
    assert _saga(fabrica, susc.id).firma_intentos_fallidos == 0


async def test_cada_accion_queda_auditada_con_su_resultado_y_sin_el_codigo(escenario, fabrica, caplog):
    api, susc, _ = escenario
    huella = await _huella(api, susc)
    codigo_erroneo = "987654"

    with caplog.at_level(logging.DEBUG):
        await api.get(_ruta(susc, "/documento-firma/pdf"), headers=ASESOR)
        await api.post(_ruta(susc, "/firma/codigo"), headers=ASESOR)
        await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella, codigo=codigo_erroneo), headers=ASESOR)
        await api.post(_ruta(susc, "/firma"), json=_cuerpo(huella), headers=ASESOR)
        await api.get(_ruta(susc, "/firma/documento"), headers=ASESOR)

    assert [(a.accion, a.resultado) for a in _auditoria(fabrica)] == [
        ("DESCARGAR_DOCUMENTO", "EXITO"),
        ("SOLICITAR_CODIGO", "EXITO"),
        ("FIRMAR", "OTP_INVALIDO"),
        ("FIRMAR", "EXITO"),
        ("DESCARGAR_FIRMADA", "EXITO"),
    ]
    assert all(a.asesor_id == ASESOR_ID and a.suscripcion_id == susc.id for a in _auditoria(fabrica))
    assert OTP not in caplog.text and codigo_erroneo not in caplog.text


async def test_la_auditoria_es_inmutable(escenario, fabrica):
    api, susc, _ = escenario
    await api.post(_ruta(susc, "/firma/codigo"), headers=ASESOR)

    with pytest.raises(RuntimeError, match="inmutable"), fabrica() as s, s.begin():
        s.get(AuditoriaAsesorDB, _auditoria(fabrica)[0].id).resultado = "OTRO"


async def test_las_rutas_del_cliente_no_cambian(escenario):
    api, susc, _ = escenario

    r = await api.get(f"/v1/suscripciones/{susc.id}/documento-firma", headers={"Authorization": "Bearer token-1"})

    assert r.status_code == 200
