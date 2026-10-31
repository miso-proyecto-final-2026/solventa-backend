"""La API de Notificaciones (HU22): dispositivo, bandeja del push y envíos fallidos del operador."""

import time

import jwt
import pytest
from app.notificaciones.acceso import VerificadorJwtHs256
from app.notificaciones.api import router
from app.notificaciones.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.dobles import CorreoFalso, IdentidadFalsa, poliza_emitida
from tests.test_servicio import _recibir

TOKEN_1 = {"Authorization": "Bearer token-1"}
TOKEN_2 = {"Authorization": "Bearer token-2"}
SECRETO = "secreto-de-prueba-de-notificaciones-32b"
EMISOR = "solventa-identidad"
DISPOSITIVO = {"dispositivo_id": "telefono-123456", "idioma": "en-US"}


def _jwt(rol="OPERADOR", **cambios):
    claims = {"sub": "op-1", "rol": rol, "iss": EMISOR, "exp": int(time.time()) + 600, "nombre": "Olga"} | cambios
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRETO, algorithm="HS256")}


@pytest.fixture
async def api(fabrica, reloj):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.identidad = IdentidadFalsa()
    app.state.fabrica = fabrica
    app.state.reloj = reloj
    app.state.verificador_operador = VerificadorJwtHs256(SECRETO, EMISOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _con_notificacion(armar, fabrica, api=None, **opciones):
    """Emite una póliza. Con `api`, el cliente 1 registra antes su dispositivo: la bandeja solo entrega lo posterior."""
    if api is not None:
        await api.post("/v1/dispositivos", json={**DISPOSITIVO, "idioma": "es-CO"}, headers=TOKEN_1)
    servicio = armar(**opciones)
    await _recibir(servicio, fabrica, poliza_emitida())
    await servicio.procesar_pendientes()


# --- dispositivo -----------------------------------------------------------------------------------------------------

async def test_registrar_el_dispositivo_guarda_su_idioma_y_es_idempotente(api):
    primera = await api.post("/v1/dispositivos", json=DISPOSITIVO, headers=TOKEN_1)
    segunda = await api.post("/v1/dispositivos", json={**DISPOSITIVO, "idioma": "es-CO"}, headers=TOKEN_1)

    assert primera.status_code == segunda.status_code == 200
    assert segunda.json() == {"dispositivo_id": "telefono-123456", "idioma": "es-CO"}


@pytest.mark.parametrize(
    "cuerpo",
    [
        {},
        {"dispositivo_id": "corto", "idioma": "es-CO"},
        {"dispositivo_id": "telefono 123 con espacios", "idioma": "es-CO"},
        {"dispositivo_id": "telefono-123456", "idioma": "fr-FR"},
        {"dispositivo_id": "telefono-123456", "idioma": "es-CO", "extra": 1},
    ],
)
async def test_un_dispositivo_invalido_se_rechaza_con_la_estructura_estandar(api, cuerpo):
    r = await api.post("/v1/dispositivos", json=cuerpo, headers=TOKEN_1)

    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR" and r.json()["error"]["trace_id"]


async def test_el_dispositivo_exige_una_sesion_de_cliente_valida(api):
    assert (await api.post("/v1/dispositivos", json=DISPOSITIVO)).status_code == 401
    r = await api.post("/v1/dispositivos", json=DISPOSITIVO, headers={"Authorization": "Bearer desconocido"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "NO_AUTENTICADO"


# --- bandeja del push (criterio 2) -----------------------------------------------------------------------------------

async def test_el_cliente_ve_su_notificacion_con_prioridad_alta_y_su_version_publica(api, armar, fabrica):
    await _con_notificacion(armar, fabrica, api)

    r = await api.get("/v1/notificaciones", headers=TOKEN_1)

    (n,) = r.json()
    assert r.status_code == 200
    assert (n["titulo"], n["prioridad"], n["recibida"], n["suscripcion_id"], n["idioma"]) == (
        "Suscripción completada", "ALTA", False, "susc-1", "es-CO",
    )
    assert n["titulo_publico"] == "Solventa" and n["texto_publico"] == "Tienes una notificación nueva."


async def test_cada_cliente_ve_solo_las_suyas(api, armar, fabrica):
    await _con_notificacion(armar, fabrica, api)

    assert (await api.get("/v1/notificaciones", headers=TOKEN_2)).json() == []


async def test_confirmar_la_recepcion_la_saca_de_las_nuevas_pero_la_deja_en_el_historial(api, armar, fabrica):
    await _con_notificacion(armar, fabrica, api)
    (n,) = (await api.get("/v1/notificaciones", headers=TOKEN_1)).json()

    r = await api.post(f"/v1/notificaciones/{n['id']}/recibida", headers=TOKEN_1)
    repetida = await api.post(f"/v1/notificaciones/{n['id']}/recibida", headers=TOKEN_1)

    assert r.status_code == repetida.status_code == 204
    assert (await api.get("/v1/notificaciones", headers=TOKEN_1)).json() == []
    (historial,) = (await api.get("/v1/notificaciones", params={"solo_nuevas": "false"}, headers=TOKEN_1)).json()
    assert historial["recibida"] is True


async def test_confirmar_una_notificacion_ajena_o_inexistente_responde_igual(api, armar, fabrica):
    await _con_notificacion(armar, fabrica, api)
    (n,) = (await api.get("/v1/notificaciones", headers=TOKEN_1)).json()

    ajena = await api.post(f"/v1/notificaciones/{n['id']}/recibida", headers=TOKEN_2)
    inexistente = await api.post("/v1/notificaciones/no-existe/recibida", headers=TOKEN_2)

    assert ajena.status_code == inexistente.status_code == 404
    assert ajena.json()["error"]["code"] == inexistente.json()["error"]["code"] == "NOTIFICACION_NO_ENCONTRADA"
    assert (await api.get("/v1/notificaciones", headers=TOKEN_1)).json()[0]["recibida"] is False  # no se marcó


async def test_la_bandeja_exige_sesion(api):
    assert (await api.get("/v1/notificaciones")).status_code == 401
    assert (await api.post("/v1/notificaciones/x/recibida")).status_code == 401


async def test_el_limite_de_la_lista_se_valida(api):
    for limite in (0, 101):
        assert (await api.get("/v1/notificaciones", params={"limite": limite}, headers=TOKEN_1)).status_code == 422


# --- envíos fallidos del operador (criterio 6) -----------------------------------------------------------------------

async def _con_envio_fallido(armar, fabrica, reloj):
    servicio = armar(correo=CorreoFalso(fallos=99), max_intentos=2)
    await _recibir(servicio, fabrica, poliza_emitida())
    for _ in range(2):
        await servicio.procesar_pendientes()
        reloj.avanzar(60)


async def test_el_operador_ve_los_envios_que_agotaron_sus_reintentos(api, armar, fabrica, reloj):
    await _con_envio_fallido(armar, fabrica, reloj)

    r = await api.get("/v1/notificaciones/envios-fallidos", headers=_jwt())

    (e,) = r.json()
    assert r.status_code == 200
    assert (e["canal"], e["tipo"], e["cliente_id"], e["intentos"], e["ultimo_error"]) == (
        "CORREO", "POLIZA_EMITIDA", "cli-1", 2, "MessageRejected",
    )
    assert "ana.perez@example.com" not in r.text and "36GBQKV2CWMM3EBB8Q6Q" not in r.text


async def test_sin_fallos_la_lista_esta_vacia(api):
    assert (await api.get("/v1/notificaciones/envios-fallidos", headers=_jwt())).json() == []


@pytest.mark.parametrize(
    ("cabeceras", "estado"),
    [
        ({}, 401),
        (TOKEN_1, 401),  # una sesión de cliente no es un JWT de operador
        ({"Authorization": "Bearer " + jwt.encode({"sub": "x", "rol": "OPERADOR", "iss": EMISOR, "exp": int(time.time()) - 5}, SECRETO, "HS256")}, 401),
        ({"Authorization": "Bearer " + jwt.encode({"sub": "x", "rol": "OPERADOR", "iss": "otro", "exp": int(time.time()) + 60}, SECRETO, "HS256")}, 401),
        ({"Authorization": "Bearer " + jwt.encode({"sub": "x", "rol": "OPERADOR", "iss": EMISOR, "exp": int(time.time()) + 60}, "otro-secreto-de-32-bytes-o-mas!!", "HS256")}, 401),
        (_jwt(rol="ASESOR"), 403),
        (_jwt(rol="CLIENTE"), 403),
    ],
)
async def test_solo_un_operador_con_un_jwt_valido_ve_los_envios_fallidos(api, cabeceras, estado):
    r = await api.get("/v1/notificaciones/envios-fallidos", headers=cabeceras)

    assert r.status_code == estado and r.json()["error"]["trace_id"]


async def test_lo_anterior_al_dispositivo_no_se_entrega_como_nuevo_pero_queda_en_el_historial(api, armar, fabrica, reloj):
    await _con_notificacion(armar, fabrica)  # la póliza se emitió antes de que el cliente registrara su teléfono
    reloj.avanzar(60)
    await api.post("/v1/dispositivos", json=DISPOSITIVO, headers=TOKEN_1)

    assert (await api.get("/v1/notificaciones", headers=TOKEN_1)).json() == []
    assert len((await api.get("/v1/notificaciones", params={"solo_nuevas": "false"}, headers=TOKEN_1)).json()) == 1


async def test_sin_dispositivo_registrado_no_hay_nada_nuevo_que_entregar(api, armar, fabrica):
    await _con_notificacion(armar, fabrica)

    assert (await api.get("/v1/notificaciones", headers=TOKEN_1)).json() == []
