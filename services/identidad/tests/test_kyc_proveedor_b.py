"""Adaptador del proveedor B (HA06): su contrato propio, la selección por configuración y el flujo completo."""

import base64
import json

import httpx
import pytest

from app.kyc.config import ConfigKyc
from app.kyc.dominio import ProveedorKycError
from app.kyc.proveedor_b import ConfigProveedorB, VerificadorProveedorB
from app.kyc.servicio import ServicioVerificacion
from app.kyc.simulador import VerificadorSimulado
from app.kyc.wiring import construir_verificador, obtener_servicio_kyc
from app.main import app
from tests.contrato_verificador import MARCA_IMAGEN, solicitud
from tests.helpers import registrar
from tests.proveedor_b_falso import ModoB, ProveedorBFalso
from tests.test_kyc_api import AlmacenMemoria, Reloj, enviar, estado

URL = "https://kyc-b.test"


def _adaptador(proveedor: ProveedorBFalso | None = None, api_key: str = "clave-b"):
    proveedor = proveedor or ProveedorBFalso()
    cliente = httpx.AsyncClient(base_url=URL, transport=proveedor.transport)
    return VerificadorProveedorB(ConfigProveedorB(URL, api_key), cliente), proveedor


# -- el contrato propio de B ---------------------------------------------------------------------


async def test_envia_la_peticion_con_el_formato_y_la_autenticacion_de_b():
    verificador, proveedor = _adaptador()

    await verificador.verificar(solicitud(clave="clave-xyz"))

    (peticion,) = proveedor.peticiones
    assert peticion.method == "POST"
    assert peticion.url.path == "/v2/identity-checks"
    assert peticion.headers["X-Api-Key"] == "clave-b"
    cuerpo = json.loads(peticion.content)
    assert cuerpo["reference"] == "clave-xyz"
    assert cuerpo["challenge"] == "TURN_LEFT"  # el reto del dominio traducido al vocabulario de B
    assert base64.b64decode(cuerpo["documents"]["front"]) == MARCA_IMAGEN + b"-a"
    assert base64.b64decode(cuerpo["documents"]["back"]) == MARCA_IMAGEN + b"-r"
    assert base64.b64decode(cuerpo["selfie"]) == MARCA_IMAGEN + b"-s"


async def test_una_clave_invalida_es_error_del_proveedor_y_no_revela_la_clave():
    verificador, _ = _adaptador(api_key="clave-equivocada")

    with pytest.raises(ProveedorKycError) as error:
        await verificador.verificar(solicitud())

    assert "401" in str(error.value) and "INVALID_API_KEY" in str(error.value)
    assert "clave-equivocada" not in str(error.value)


async def test_una_revision_manual_de_b_deja_el_caso_en_revision():
    # REVIEW no existe en el dominio: se degrada como un fallo del proveedor (EN_REVISION).
    verificador, _ = _adaptador(ProveedorBFalso(modo=ModoB.REVIEW))

    with pytest.raises(ProveedorKycError, match="no concluyente"):
        await verificador.verificar(solicitud())


async def test_un_resultado_desconocido_no_se_interpreta_como_aprobado():
    def respuesta(peticion):
        return httpx.Response(200, json={"id": "chk_1", "outcome": "MAYBE"})

    cliente = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(respuesta))
    verificador = VerificadorProveedorB(ConfigProveedorB(URL, "k"), cliente)

    with pytest.raises(ProveedorKycError):
        await verificador.verificar(solicitud())


@pytest.mark.parametrize("excepcion", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_los_fallos_de_red_se_traducen_a_error_del_dominio(excepcion):
    def falla(peticion):
        raise excepcion("fallo", request=peticion)

    cliente = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(falla))
    verificador = VerificadorProveedorB(ConfigProveedorB(URL, "k"), cliente)

    with pytest.raises(ProveedorKycError):
        await verificador.verificar(solicitud())


async def test_un_cuerpo_que_no_es_json_es_error_del_dominio():
    cliente = httpx.AsyncClient(
        base_url=URL, transport=httpx.MockTransport(lambda p: httpx.Response(200, text="<html>"))
    )
    verificador = VerificadorProveedorB(ConfigProveedorB(URL, "k"), cliente)

    with pytest.raises(ProveedorKycError):
        await verificador.verificar(solicitud())


# -- configuración y selección -----------------------------------------------------------------


def test_la_configuracion_de_b_sale_del_entorno():
    config = ConfigProveedorB.desde_entorno(
        {"KYC_PROVEEDOR_B_URL": URL, "KYC_PROVEEDOR_B_API_KEY": "k", "KYC_PROVEEDOR_B_TIMEOUT_S": "2.5"}
    )

    assert (config.base_url, config.api_key, config.timeout_s) == (URL, "k", 2.5)
    assert ConfigProveedorB.desde_entorno({"KYC_PROVEEDOR_B_URL": URL, "KYC_PROVEEDOR_B_API_KEY": "k"}).timeout_s == 4.0


def test_sin_url_o_clave_no_se_puede_usar_el_proveedor_b():
    with pytest.raises(ValueError, match="KYC_PROVEEDOR_B_API_KEY"):
        ConfigProveedorB.desde_entorno({"KYC_PROVEEDOR_B_URL": URL})
    with pytest.raises(ValueError, match="KYC_PROVEEDOR_B_URL"):
        ConfigProveedorB.desde_entorno({})


def test_por_defecto_se_usa_el_proveedor_simulado():
    assert isinstance(construir_verificador({}), VerificadorSimulado)
    assert isinstance(construir_verificador({"KYC_PROVEEDOR": "simulado"}), VerificadorSimulado)


def test_el_proveedor_b_se_elige_solo_por_configuracion():
    env = {"KYC_PROVEEDOR": "Proveedor_B", "KYC_PROVEEDOR_B_URL": URL, "KYC_PROVEEDOR_B_API_KEY": "k"}

    assert isinstance(construir_verificador(env), VerificadorProveedorB)


def test_un_proveedor_desconocido_falla_con_un_mensaje_claro():
    with pytest.raises(ValueError, match="KYC_PROVEEDOR desconocido"):
        construir_verificador({"KYC_PROVEEDOR": "otro"})


def test_la_dependencia_de_la_api_respeta_la_configuracion(monkeypatch):
    monkeypatch.setenv("KYC_PROVEEDOR", "proveedor_b")
    monkeypatch.setenv("KYC_PROVEEDOR_B_URL", URL)
    monkeypatch.setenv("KYC_PROVEEDOR_B_API_KEY", "k")
    obtener_servicio_kyc.cache_clear()
    try:
        assert isinstance(obtener_servicio_kyc()._verificador, VerificadorProveedorB)
    finally:
        obtener_servicio_kyc.cache_clear()


# -- el núcleo funciona igual con B -------------------------------------------------------------


class EntornoB:
    def __init__(self, modo: ModoB):
        self.proveedor = ProveedorBFalso(modo=modo)
        cliente = httpx.AsyncClient(base_url=URL, transport=self.proveedor.transport)
        verificador = VerificadorProveedorB(ConfigProveedorB(URL, self.proveedor.api_key), cliente)
        self.almacen = AlmacenMemoria()
        self.servicio = ServicioVerificacion(verificador, self.almacen, ConfigKyc(timeout_s=0.2), Reloj())
        app.dependency_overrides[obtener_servicio_kyc] = lambda: self.servicio


@pytest.fixture
def entorno_b():
    yield EntornoB
    app.dependency_overrides.pop(obtener_servicio_kyc, None)


def test_con_el_proveedor_b_se_verifica_al_cliente_por_la_api(client, entorno_b):
    entorno_b(ModoB.PASS)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.status_code == 201 and r.json()["estado"] == "VERIFICADO"
    assert estado(client, cliente_id, auth).json()["estado_kyc"] == "VERIFICADO"


def test_con_el_proveedor_b_un_rechazo_descuenta_un_intento(client, entorno_b):
    entorno_b(ModoB.FAIL)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.json()["estado"] == "RECHAZADO" and r.json()["intentos_restantes"] == 2


@pytest.mark.parametrize("modo", [ModoB.ERROR_503, ModoB.CAIDO, ModoB.HANG, ModoB.REVIEW, ModoB.VACIO])
def test_con_el_proveedor_b_cualquier_fallo_deja_el_caso_en_revision(client, entorno_b, modo):
    env = entorno_b(modo)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.status_code == 201 and r.json()["estado"] == "EN_REVISION"
    assert len(env.almacen.objetos) == 3  # las imágenes se conservan


def test_con_el_proveedor_b_reenviar_no_abre_otro_caso(client, entorno_b):
    env = entorno_b(ModoB.PASS)
    cliente_id, auth = registrar(client)

    enviar(client, cliente_id, auth, clave="misma-clave")
    enviar(client, cliente_id, auth, clave="misma-clave")

    assert len(env.proveedor.peticiones) == 1
    assert len(env.proveedor.casos) == 1
