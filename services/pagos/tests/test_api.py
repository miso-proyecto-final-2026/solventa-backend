"""Contrato HTTP interno de Pagos (criterio 8 y entradas internas `cobros` y `tokenizar`)."""

import pytest
from app.pagos.api import router
from app.pagos.errores import registrar_errores
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.correlacion import MiddlewareCorrelacion
from tests.dobles import TOKEN, PasarelaFalsa

CLAVE = "clave-saga"
CABECERAS = {"X-Service-Key": CLAVE}
COBRO = {"suscripcion_id": "susc-1", "cliente_id": "cli-1", "metodo_pago": TOKEN, "monto": "175000.00"}
PAN_PRUEBA = "4111111111111111"  # número de prueba público de Visa, no una tarjeta real


@pytest.fixture
def pasarela():
    return PasarelaFalsa()


@pytest.fixture
async def cliente(armar, pasarela):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.add_middleware(MiddlewareCorrelacion)
    app.state.servicio_cobros = armar(pasarela)
    app.state.api_keys = {CLAVE: "suscripcion"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.app = app
        yield c


# --- autenticación ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "metodo, ruta",
    [("get", "/v1/pagos/transacciones/x"), ("post", "/v1/pagos/cobros"), ("post", "/v1/pagos/tokenizar")],
)
async def test_sin_clave_de_servicio_responde_401_y_con_una_ajena_403(cliente, metodo, ruta):
    cuerpo = {"json": COBRO} if metodo == "post" else {}
    sin = await getattr(cliente, metodo)(ruta, **cuerpo)
    ajena = await getattr(cliente, metodo)(ruta, headers={"X-Service-Key": "otra"}, **cuerpo)
    assert (sin.status_code, sin.json()["error"]["code"]) == (401, "NO_AUTENTICADO")
    assert (ajena.status_code, ajena.json()["error"]["code"]) == (403, "SIN_PERMISO")


async def test_sin_claves_configuradas_nadie_accede(cliente):
    cliente.app.state.api_keys = {}
    r = await cliente.get("/v1/pagos/transacciones/x", headers=CABECERAS)
    assert r.status_code == 403


# --- cobros ----------------------------------------------------------------------------------------------------

async def test_post_cobros_cobra_y_devuelve_la_transaccion_aprobada(cliente):
    r = await cliente.post("/v1/pagos/cobros", json=COBRO, headers=CABECERAS)

    assert r.status_code == 201
    cuerpo = r.json()
    assert (cuerpo["tipo"], cuerpo["estado"], cuerpo["monto"], cuerpo["moneda"]) == ("COBRO_PRIMA", "APROBADA", "175000.00", "COP")
    assert cuerpo["referencia_pasarela"] == "ref-1" and cuerpo["ultimos4"] == "4242" and cuerpo["rechazo"] is None
    assert TOKEN not in r.text


async def test_repetir_la_clave_devuelve_200_con_el_mismo_resultado_sin_cobrar(cliente, pasarela):
    cabeceras = {**CABECERAS, "Idempotency-Key": "k-1"}
    primera = await cliente.post("/v1/pagos/cobros", json=COBRO, headers=cabeceras)
    segunda = await cliente.post("/v1/pagos/cobros", json=COBRO, headers=cabeceras)

    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert segunda.headers["Idempotent-Replay"] == "true"
    assert segunda.json()["id"] == primera.json()["id"] and pasarela.cargos == 1


async def test_la_clave_con_otro_monto_responde_409(cliente):
    await cliente.post("/v1/pagos/cobros", json=COBRO, headers=CABECERAS)
    r = await cliente.post("/v1/pagos/cobros", json={**COBRO, "monto": "1.00"}, headers=CABECERAS)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "CLAVE_IDEMPOTENCIA_REUTILIZADA")


async def test_un_rechazo_devuelve_codigo_motivo_y_gracia(armar):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_cobros = armar(PasarelaFalsa("rechazar"))
    app.state.api_keys = {CLAVE: "suscripcion"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/pagos/cobros", json=COBRO, headers=CABECERAS)
    cuerpo = r.json()
    assert cuerpo["estado"] == "RECHAZADA" and cuerpo["rechazo"]["codigo"] == "FONDOS_INSUFICIENTES"
    assert cuerpo["gracia_hasta"] is not None


async def test_sin_respuesta_de_la_pasarela_responde_202_pendiente(armar):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_cobros = armar(PasarelaFalsa("perder"))
    app.state.api_keys = {CLAVE: "suscripcion"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/pagos/cobros", json=COBRO, headers=CABECERAS)
    assert (r.status_code, r.json()["estado"]) == (202, "PENDIENTE")


@pytest.mark.parametrize(
    "cambios",
    [{"monto": "0"}, {"moneda": "USD"}, {"periodicidad_pago": "SEMANAL"}, {"numero_tarjeta": PAN_PRUEBA}],
)
async def test_un_cobro_invalido_responde_422_con_el_formato_estandar(cliente, cambios):
    r = await cliente.post("/v1/pagos/cobros", json={**COBRO, **cambios}, headers=CABECERAS)
    error = r.json()["error"]
    assert r.status_code == 422 and error["code"] == "VALIDATION_ERROR" and error["trace_id"]
    assert PAN_PRUEBA not in r.text


async def test_un_pan_como_metodo_de_pago_se_rechaza(cliente, pasarela):
    r = await cliente.post("/v1/pagos/cobros", json={**COBRO, "metodo_pago": "4111 1111 1111 1111"}, headers=CABECERAS)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "DATO_DE_TARJETA_NO_PERMITIDO")
    assert pasarela.ordenes == []


# --- tokenizar -------------------------------------------------------------------------------------------------

async def test_tokenizar_registra_el_token_y_devuelve_marca_y_ultimos_4_sin_el_token(cliente):
    r = await cliente.post("/v1/pagos/tokenizar", json={"token": TOKEN, "cliente_id": "cli-1"}, headers=CABECERAS)
    assert r.status_code == 201
    assert {k: v for k, v in r.json().items() if k != "token_id"} == {"ultimos4": "4242", "marca": "VISA"}
    assert TOKEN not in r.text


async def test_tokenizar_dos_veces_el_mismo_token_no_lo_duplica(cliente):
    a = await cliente.post("/v1/pagos/tokenizar", json={"token": TOKEN}, headers=CABECERAS)
    b = await cliente.post("/v1/pagos/tokenizar", json={"token": TOKEN}, headers=CABECERAS)
    assert a.json()["token_id"] == b.json()["token_id"]


@pytest.mark.parametrize("cuerpo", [{"token": PAN_PRUEBA}, {"token": "4111-1111-1111-1111"}, {"token": TOKEN, "pan": PAN_PRUEBA}])
async def test_tokenizar_no_acepta_el_numero_de_tarjeta(cliente, cuerpo):
    r = await cliente.post("/v1/pagos/tokenizar", json=cuerpo, headers=CABECERAS)
    assert r.status_code == 422 and PAN_PRUEBA not in r.text and "4111-1111" not in r.text


async def test_tokenizar_un_token_que_la_pasarela_no_reconoce_responde_422(cliente):
    r = await cliente.post("/v1/pagos/tokenizar", json={"token": "desconocido"}, headers=CABECERAS)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "TOKEN_NO_RECONOCIDO")


# --- consulta (criterio 8) -------------------------------------------------------------------------------------

async def test_get_devuelve_el_estado_de_la_transaccion(cliente):
    creada = (await cliente.post("/v1/pagos/cobros", json=COBRO, headers=CABECERAS)).json()

    r = await cliente.get(f"/v1/pagos/transacciones/{creada['id']}", headers=CABECERAS)

    assert r.status_code == 200 and r.json() == creada
    assert TOKEN not in r.text


async def test_get_de_una_transaccion_inexistente_responde_404_con_trace_id(cliente):
    r = await cliente.get("/v1/pagos/transacciones/no-existe", headers=CABECERAS)
    error = r.json()["error"]
    assert (r.status_code, error["code"]) == (404, "TRANSACCION_NO_ENCONTRADA")
    assert set(error) == {"code", "message", "details", "trace_id"} and error["trace_id"]
