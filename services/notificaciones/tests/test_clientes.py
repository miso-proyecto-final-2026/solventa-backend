"""Clientes HTTP de Identidad y Suscripción, probados con un transporte falso (sin red)."""

import httpx
import pytest
from app.notificaciones.clientes import ClienteIdentidad, ClienteSuscripcion
from app.notificaciones.errores import DependenciaNoDisponible

CLAVE = "clave-de-servicio-de-prueba"


def _http(manejador) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(manejador), base_url="http://vecino")


# --- Identidad -------------------------------------------------------------------------------------------------------

async def test_la_sesion_se_valida_con_la_clave_de_servicio_y_el_token_del_cliente():
    vistas = []

    def manejador(peticion):
        vistas.append(peticion)
        return httpx.Response(200, json={"cliente_id": "cli-1"})

    cliente = await ClienteIdentidad(_http(manejador), CLAVE).cliente_de_token("token-1")

    assert cliente == "cli-1"
    assert vistas[0].headers["x-service-key"] == CLAVE and vistas[0].headers["authorization"] == "Bearer token-1"


async def test_un_token_rechazado_no_es_un_cliente():
    assert await ClienteIdentidad(_http(lambda _: httpx.Response(401)), CLAVE).cliente_de_token("malo") is None


async def test_el_contacto_trae_nombre_y_correo():
    cuerpo = {"cliente_id": "cli-1", "nombre": "Ana", "email": "ana@example.com"}

    contacto = await ClienteIdentidad(_http(lambda _: httpx.Response(200, json=cuerpo)), CLAVE).contacto("cli-1")

    assert (contacto.cliente_id, contacto.nombre, contacto.email) == ("cli-1", "Ana", "ana@example.com")


async def test_un_cliente_inexistente_no_tiene_contacto():
    assert await ClienteIdentidad(_http(lambda _: httpx.Response(404)), CLAVE).contacto("no-existe") is None


@pytest.mark.parametrize("llamada", ["cliente_de_token", "contacto"])
async def test_si_identidad_falla_o_no_responde_se_informa_dependencia_no_disponible(llamada):
    def cae(_):
        raise httpx.ConnectError("sin red")

    for http in (_http(lambda _: httpx.Response(500)), _http(cae)):
        with pytest.raises(DependenciaNoDisponible):
            await getattr(ClienteIdentidad(http, CLAVE), llamada)("x")


# --- Suscripción -----------------------------------------------------------------------------------------------------

async def test_el_enlace_se_pide_con_la_clave_de_servicio_y_el_idioma():
    vistas = []

    def manejador(peticion):
        vistas.append(peticion)
        return httpx.Response(201, json={"ruta": "/v1/certificados/abc", "vence_en": "2026-10-31T20:43:00.437375Z"})

    enlace = await ClienteSuscripcion(_http(manejador), CLAVE).emitir_enlace("pol-1", "en-US")

    assert enlace.ruta == "/v1/certificados/abc" and enlace.vence_en.isoformat().startswith("2026-10-31T20:43:00")
    assert enlace.vence_en.tzinfo is not None
    assert vistas[0].headers["x-service-key"] == CLAVE
    assert vistas[0].url.path == "/v1/interno/certificados/enlaces" and b'"en-US"' in vistas[0].content


async def test_si_suscripcion_no_tiene_habilitados_los_enlaces_no_hay_enlace_pero_tampoco_error():
    respuesta = httpx.Response(503, json={"error": {"code": "ENLACES_NO_CONFIGURADOS", "message": "x", "details": []}})

    assert await ClienteSuscripcion(_http(lambda _: respuesta), CLAVE).emitir_enlace("pol-1", "es-CO") is None


@pytest.mark.parametrize("estado", [401, 404, 500, 503])
async def test_cualquier_otra_respuesta_de_suscripcion_es_un_fallo_que_se_reintenta(estado):
    respuesta = httpx.Response(estado, json={"error": {"code": "OTRO", "message": "x", "details": []}})

    with pytest.raises(DependenciaNoDisponible):
        await ClienteSuscripcion(_http(lambda _: respuesta), CLAVE).emitir_enlace("pol-1", "es-CO")


async def test_si_suscripcion_no_responde_es_un_fallo_que_se_reintenta():
    def cae(_):
        raise httpx.ReadTimeout("lento")

    with pytest.raises(DependenciaNoDisponible):
        await ClienteSuscripcion(_http(cae), CLAVE).emitir_enlace("pol-1", "es-CO")
