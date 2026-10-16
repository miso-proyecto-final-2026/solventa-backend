"""Adaptador de consentimiento hacia MS Identidad (HU06): contrato y falla cerrada."""

import httpx
import pytest

from app.proveedores.consentimiento import ConsentimientoHttp
from app.proveedores.dominio import EstadoConsentimiento

BASE = "http://identidad.test"


def _adaptador(manejador, api_key="clave-servicio"):
    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(manejador))
    return ConsentimientoHttp(BASE, api_key, cliente=http)


@pytest.mark.parametrize("estado", ["OTORGADO", "REVOCADO", "NO_OTORGADO"])
async def test_traduce_los_tres_estados_del_contrato(estado):
    def manejador(request):
        return httpx.Response(200, json={"cliente_id": "c1", "estado": estado})

    assert await _adaptador(manejador).estado("c1") is EstadoConsentimiento(estado)


async def test_consulta_la_ruta_del_contrato_con_la_clave_de_servicio():
    vistas = []

    def manejador(request):
        vistas.append(request)
        return httpx.Response(200, json={"estado": "OTORGADO"})

    await _adaptador(manejador).estado("cliente/raro")

    assert vistas[0].method == "GET"
    assert vistas[0].url.raw_path == (
        b"/v1/clientes/cliente%2Fraro/consentimientos/OPEN_FINANCE_OPEN_DATA"
    )
    assert vistas[0].headers["X-Service-Key"] == "clave-servicio"


async def test_cliente_desconocido_es_no_otorgado():
    assert (
        await _adaptador(lambda r: httpx.Response(404)).estado("c1")
        is EstadoConsentimiento.NO_OTORGADO
    )


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_falla_cerrado_ante_respuestas_de_error(status):
    adaptador = _adaptador(lambda r: httpx.Response(status))

    assert await adaptador.estado("c1") is EstadoConsentimiento.NO_OTORGADO


async def test_falla_cerrado_si_identidad_no_responde():
    def manejador(request):
        raise httpx.ConnectTimeout("sin respuesta")

    assert await _adaptador(manejador).estado("c1") is EstadoConsentimiento.NO_OTORGADO


@pytest.mark.parametrize("cuerpo", [{"estado": "OTRO"}, {}, [], "texto"])
async def test_falla_cerrado_ante_cuerpo_invalido(cuerpo):
    adaptador = _adaptador(lambda r: httpx.Response(200, json=cuerpo))

    assert await adaptador.estado("c1") is EstadoConsentimiento.NO_OTORGADO


def test_sin_identidad_url_no_se_construye_el_adaptador():
    assert ConsentimientoHttp.desde_entorno({}) is None


def test_se_configura_desde_el_entorno():
    adaptador = ConsentimientoHttp.desde_entorno(
        {"IDENTIDAD_URL": BASE, "IDENTIDAD_SERVICE_KEY": "k", "IDENTIDAD_TIMEOUT_S": "0.2"}
    )

    assert adaptador is not None
    assert adaptador._http.timeout.read == 0.2
