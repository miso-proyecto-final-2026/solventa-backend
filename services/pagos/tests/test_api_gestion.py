"""API de cobros fallidos (HU19): JWT de asesor u operador, 403 sin rol, sin datos de tarjeta y errores distinguibles."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.pagos.acceso import VerificadorJwtHs256
from app.pagos.api import router as router_interno
from app.pagos.api_gestion import router
from app.pagos.errores import registrar_errores
from app.pagos.gestion import ServicioGestion
from app.pagos.servicio import ComandoCobro
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.correlacion import MiddlewareCorrelacion
from tests.dobles import TOKEN, PasarelaFalsa, comando

SECRETO = "secreto-de-prueba-hu19-de-32-bytes-o-mas"
EMISOR = "solventa-identidad"
RUTA = "/v1/pagos/cobros-fallidos"
PAN_PRUEBA = "4111111111111111"  # número de prueba público de Visa, no una tarjeta real


def token(rol="ASESOR", *, secreto=SECRETO, vence_en_s=1800, sub="u-1"):
    ahora = datetime.now(UTC)
    claims = {"sub": sub, "rol": rol, "nombre": f"{rol.title()} Demo", "iss": EMISOR, "iat": ahora,
              "exp": ahora + timedelta(seconds=vence_en_s)}
    return {"Authorization": f"Bearer {jwt.encode(claims, secreto, algorithm='HS256')}"}


@pytest.fixture
def pasarela():
    return PasarelaFalsa("rechazar")


@pytest.fixture
async def cliente(armar, fabrica, reloj, pasarela):
    cobros = armar(pasarela)
    app = FastAPI()
    app.include_router(router_interno)
    app.include_router(router)
    registrar_errores(app)
    app.add_middleware(MiddlewareCorrelacion)
    app.state.servicio_cobros = cobros
    app.state.servicio_gestion = ServicioGestion(fabrica=fabrica, cobros=cobros, horas_por_vencer=24, reloj=reloj)
    app.state.verificador_gestion = VerificadorJwtHs256(SECRETO, EMISOR)
    app.state.api_keys = {"clave-saga": "suscripcion"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.cobros = cobros
        yield c


async def _caso(cliente, suscripcion_id="susc-1"):
    tx, _ = await cliente.cobros.cobrar(ComandoCobro.desde_evento(comando(suscripcion_id)))
    return tx.id


# --- permisos (criterio 7) -------------------------------------------------------------------------------------

RUTAS = [("get", RUTA), ("get", f"{RUTA}/x"), ("post", f"{RUTA}/x/notas"), ("post", f"{RUTA}/x/reintentar")]


@pytest.mark.parametrize("metodo, ruta", RUTAS)
async def test_sin_token_o_con_uno_invalido_o_vencido_es_401(cliente, metodo, ruta):
    cuerpo = {"json": {"texto": "hola"}} if ruta.endswith("notas") else {}
    for cabeceras in ({}, token(secreto="otro-secreto-tambien-de-32-bytes-o-mas"), token(vence_en_s=-10), {"Authorization": "Basic abc"}):
        r = await getattr(cliente, metodo)(ruta, headers=cabeceras, **cuerpo)
        assert (r.status_code, r.json()["error"]["code"]) == (401, "NO_AUTENTICADO")


@pytest.mark.parametrize("metodo, ruta", RUTAS)
async def test_sin_rol_de_asesor_u_operador_es_403(cliente, metodo, ruta):
    cuerpo = {"json": {"texto": "hola"}} if ruta.endswith("notas") else {}
    r = await getattr(cliente, metodo)(ruta, headers=token("AUDITOR"), **cuerpo)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "SIN_PERMISO")


async def test_la_clave_de_servicio_no_abre_la_gestion(cliente):
    r = await cliente.get(RUTA, headers={"X-Service-Key": "clave-saga"})
    assert r.status_code == 401


@pytest.mark.parametrize("rol", ["ASESOR", "OPERADOR"])
async def test_asesor_y_operador_ven_la_bandeja(cliente, rol):
    caso_id = await _caso(cliente)
    r = await cliente.get(RUTA, headers=token(rol))
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["items"]] == [caso_id]


async def test_el_jwt_del_asesor_no_abre_los_endpoints_internos(cliente):
    r = await cliente.get("/v1/pagos/transacciones/x", headers=token())
    assert r.status_code == 401


# --- bandeja y detalle (criterios 1 a 3) -----------------------------------------------------------------------

async def test_la_bandeja_trae_monto_intentos_motivo_gracia_y_solo_los_ultimos_4(cliente):
    await _caso(cliente)
    r = await cliente.get(RUTA, headers=token())

    caso = r.json()["items"][0]
    assert (caso["monto"], caso["intentos"], caso["codigo_rechazo"], caso["motivo_rechazo"]) == (
        "175000.00", 1, "FONDOS_INSUFICIENTES", "Sin fondos",
    )
    assert (caso["estado"], caso["estado_gracia"]) == ("PENDIENTE", "VIGENTE")
    assert caso["tarjeta"] == {"marca": "VISA", "ultimos4": "4242"}
    assert (r.json()["total"], r.json()["pagina"], r.json()["tamano"]) == (1, 1, 20)
    assert TOKEN not in r.text


@pytest.mark.parametrize("consulta", ["tamano=101", "tamano=0", "pagina=0", "estado=OTRO"])
async def test_la_bandeja_valida_la_paginacion_y_el_filtro(cliente, consulta):
    r = await cliente.get(f"{RUTA}?{consulta}", headers=token())
    assert (r.status_code, r.json()["error"]["code"]) == (422, "VALIDATION_ERROR")


async def test_la_bandeja_filtra_por_estado_y_por_vencer(cliente, reloj):
    await _caso(cliente)
    assert (await cliente.get(f"{RUTA}?estado=RECUPERADO", headers=token())).json()["total"] == 0
    assert (await cliente.get(f"{RUTA}?por_vencer=true", headers=token())).json()["total"] == 0
    reloj.avanzar(60 * 60 * 50)
    assert (await cliente.get(f"{RUTA}?por_vencer=true", headers=token())).json()["total"] == 1


async def test_el_detalle_trae_cobros_intentos_y_notas_sin_token_ni_contacto(cliente):
    caso_id = await _caso(cliente)
    await cliente.post(f"{RUTA}/{caso_id}/notas", json={"texto": "Llamar el lunes"}, headers=token("OPERADOR", sub="op-1"))

    r = await cliente.get(f"{RUTA}/{caso_id}", headers=token())

    cuerpo = r.json()
    assert r.status_code == 200
    assert [c["es_reintento"] for c in cuerpo["cobros"]] == [False]
    assert cuerpo["cobros"][0]["intentos"][0]["codigo"] == "FONDOS_INSUFICIENTES"
    assert [(n["texto"], n["autor_id"], n["autor_rol"]) for n in cuerpo["notas"]] == [("Llamar el lunes", "op-1", "OPERADOR")]
    assert TOKEN not in r.text
    assert not {"email", "correo", "celular", "telefono"} & set(cuerpo)


async def test_un_caso_inexistente_es_404_con_su_codigo(cliente):
    r = await cliente.get(f"{RUTA}/no-existe", headers=token())
    assert (r.status_code, r.json()["error"]["code"]) == (404, "CASO_NO_ENCONTRADO")


# --- notas (criterio 5) ----------------------------------------------------------------------------------------

async def test_post_nota_responde_201_con_autor_y_fecha(cliente):
    caso_id = await _caso(cliente)
    r = await cliente.post(f"{RUTA}/{caso_id}/notas", json={"texto": "Cliente avisado"}, headers=token(sub="ase-9"))
    assert r.status_code == 201
    assert (r.json()["autor_id"], r.json()["autor_nombre"]) == ("ase-9", "Asesor Demo")
    assert r.json()["creada_en"]


@pytest.mark.parametrize("cuerpo", [{"texto": ""}, {"texto": f"pago con {PAN_PRUEBA}"}, {"texto": "x", "pan": "1"}])
async def test_una_nota_invalida_es_422_sin_devolver_el_valor(cliente, cuerpo):
    caso_id = await _caso(cliente)
    r = await cliente.post(f"{RUTA}/{caso_id}/notas", json=cuerpo, headers=token())
    assert r.status_code == 422
    assert PAN_PRUEBA not in r.text


# --- reintento (criterios 4 y 8) -------------------------------------------------------------------------------

async def test_reintento_aprobado_responde_200_y_un_segundo_es_409(cliente, pasarela):
    caso_id = await _caso(cliente)
    pasarela.guion = ["aprobar"]

    r = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=token())
    otro = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=token())

    assert (r.status_code, r.json()["resultado"], r.json()["estado_caso"]) == (200, "APROBADO", "RECUPERADO")
    assert (otro.status_code, otro.json()["error"]["code"]) == (409, "CASO_YA_RECUPERADO")


async def test_reintento_rechazado_trae_el_motivo(cliente, pasarela):
    caso_id = await _caso(cliente)
    pasarela.guion = ["rechazar"]

    r = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=token())

    assert (r.status_code, r.json()["resultado"], r.json()["codigo_rechazo"]) == (200, "RECHAZADO", "FONDOS_INSUFICIENTES")
    assert r.json()["estado_caso"] == "PENDIENTE"


async def test_pasarela_caida_responde_202_en_proceso_con_su_detalle(cliente, pasarela):
    caso_id = await _caso(cliente)
    pasarela.guion = ["caida"]

    r = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=token())

    assert (r.status_code, r.json()["resultado"], r.json()["detalle_pasarela"]) == (202, "EN_PROCESO", "NO_DISPONIBLE")


async def test_dos_clics_con_la_misma_idempotency_key_marcan_la_repeticion(cliente, pasarela):
    caso_id = await _caso(cliente)
    pasarela.guion = ["rechazar"]
    cabeceras = {**token(), "Idempotency-Key": "clic-1"}

    primero = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=cabeceras)
    segundo = await cliente.post(f"{RUTA}/{caso_id}/reintentar", headers=cabeceras)

    assert segundo.json()["transaccion_id"] == primero.json()["transaccion_id"]
    assert (segundo.json()["repeticion"], segundo.headers.get("Idempotent-Replay")) == (True, "true")
    assert len(pasarela.ordenes) == 2
