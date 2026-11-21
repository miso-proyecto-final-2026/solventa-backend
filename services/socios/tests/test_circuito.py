"""Resiliencia de MS Socios (HU34, criterio 5): dependencia caída, circuito abierto, semiabierto y aislamiento."""

import httpx
import pytest
from app.socios.circuito import NOTA_PERFIL_POR_DEFECTO, Circuito, EstadoCircuito
from app.socios.clientes import ClienteCotizacion, ClienteIdentidad, ClienteSuscripcion
from app.socios.errores import ErrorSocios, ServicioDegradado, ServicioNoDisponible

COTIZAR = {"producto": "microseguro_vida", "referencia_cliente": "cli-001", "datos": {"edad": 35}}
COTIZACION_OK = {
    "id": "c-1",
    "producto": "microseguro_vida",
    "moneda": "COP",
    "prima_anual": 1,
    "prima_mensual": 1,
    "vigente_hasta": "2026-12-01T00:00:00Z",
}


class Reloj:
    def __init__(self) -> None:
        self.ahora = 1000.0

    def __call__(self) -> float:
        return self.ahora


class Servicio:
    """Transporte controlable: responde con `estado` o se cae, y cuenta las llamadas que de verdad le llegan."""

    def __init__(self) -> None:
        self.estado = 201
        self.caido = False
        self.llamadas = 0

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.llamadas += 1
        if self.caido:
            raise httpx.ConnectError("sin red", request=peticion)
        if peticion.url.path == "/health":
            return httpx.Response(200)
        if self.estado == 201:
            return httpx.Response(201, json=COTIZACION_OK)
        return httpx.Response(self.estado, json={"error": {"code": "X", "message": "m", "details": [], "trace_id": "t"}})


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def servicio():
    return Servicio()


@pytest.fixture
def circuito(reloj):
    return Circuito("cotizacion", umbral_fallos=3, enfriamiento_s=30, reloj=reloj)


@pytest.fixture
def cotizacion(servicio, circuito):
    http = httpx.AsyncClient(base_url="http://cotizacion", transport=httpx.MockTransport(servicio))
    return ClienteCotizacion(http, circuito)


async def _crear(cotizacion):
    return await cotizacion.crear("c", "microseguro_vida", {})


async def _fallar(cotizacion, veces):
    for _ in range(veces):
        with pytest.raises(ServicioNoDisponible):
            await _crear(cotizacion)


async def test_servicio_caido_es_503_service_unavailable(cotizacion, servicio):
    servicio.caido = True

    with pytest.raises(ServicioNoDisponible) as error:
        await _crear(cotizacion)

    assert (error.value.status, error.value.code) == (503, "SERVICE_UNAVAILABLE")
    assert "sin red" not in error.value.mensaje


async def test_tras_n_fallos_el_circuito_abre_y_ya_no_llama_a_la_dependencia(cotizacion, servicio, circuito, reloj):
    servicio.caido = True
    await _fallar(cotizacion, 3)
    assert circuito.estado is EstadoCircuito.ABIERTO
    llamadas = servicio.llamadas
    reloj.ahora += 10

    with pytest.raises(ServicioDegradado) as error:
        await _crear(cotizacion)

    assert servicio.llamadas == llamadas
    assert (error.value.status, error.value.code) == (503, "SERVICE_DEGRADED")
    assert error.value.cabeceras["Retry-After"] == "20"
    assert error.value.mensaje.endswith(NOTA_PERFIL_POR_DEFECTO) and "20 s" in error.value.mensaje


async def test_el_mensaje_de_suscripcion_e_identidad_no_habla_del_perfil_por_defecto(reloj):
    casos = (
        (ClienteSuscripcion, "suscripcion", lambda c: c.detalle("s", "a")),
        (ClienteIdentidad, "identidad", lambda c: c.identificar("CC", "1")),
    )
    for clase, nombre, llamar in casos:
        servicio = Servicio()
        servicio.caido = True
        circuito = Circuito(nombre, umbral_fallos=1, enfriamiento_s=30, reloj=reloj)
        cliente = clase(httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(servicio)), "k", circuito)
        with pytest.raises(ServicioNoDisponible):
            await llamar(cliente)
        with pytest.raises(ServicioDegradado) as error:
            await llamar(cliente)
        assert "perfil" not in error.value.mensaje and error.value.cabeceras["Retry-After"] == "30"


async def test_tras_el_enfriamiento_un_exito_cierra_el_circuito(cotizacion, servicio, circuito, reloj):
    servicio.caido = True
    await _fallar(cotizacion, 3)
    servicio.caido = False
    reloj.ahora += 31

    r = await _crear(cotizacion)

    assert r["id"] == "c-1" and circuito.estado is EstadoCircuito.CERRADO


async def test_un_fallo_en_el_sondeo_vuelve_a_abrir_el_circuito(cotizacion, servicio, circuito, reloj):
    servicio.caido = True
    await _fallar(cotizacion, 3)
    reloj.ahora += 31

    await _fallar(cotizacion, 1)  # el sondeo falla

    assert circuito.estado is EstadoCircuito.ABIERTO
    with pytest.raises(ServicioDegradado):
        await _crear(cotizacion)


async def test_un_5xx_cuenta_como_fallo_y_un_4xx_no(cotizacion, servicio, circuito):
    servicio.estado = 422
    for _ in range(10):
        with pytest.raises(ErrorSocios) as error:
            await _crear(cotizacion)
        assert error.value.code == "INVALID_REQUEST"
    assert circuito.estado is EstadoCircuito.CERRADO

    servicio.estado = 503
    await _fallar(cotizacion, 3)
    assert circuito.estado is EstadoCircuito.ABIERTO


async def test_un_exito_reinicia_la_cuenta_de_fallos(cotizacion, servicio, circuito):
    servicio.caido = True
    await _fallar(cotizacion, 2)
    servicio.caido = False
    await _crear(cotizacion)
    servicio.caido = True
    await _fallar(cotizacion, 2)

    assert circuito.estado is EstadoCircuito.CERRADO


async def test_el_circuito_de_una_dependencia_no_afecta_a_otra(reloj):
    caida, sana = Servicio(), Servicio()
    caida.caido = True
    sana.estado = 404
    cot = ClienteCotizacion(
        httpx.AsyncClient(base_url="http://c", transport=httpx.MockTransport(caida)),
        Circuito("cotizacion", 1, 30, reloj),
    )
    sus = ClienteSuscripcion(
        httpx.AsyncClient(base_url="http://s", transport=httpx.MockTransport(sana)),
        "k",
        Circuito("suscripcion", 1, 30, reloj),
    )
    await _fallar(cot, 1)
    with pytest.raises(ServicioDegradado):
        await _crear(cot)

    with pytest.raises(ErrorSocios) as error:
        await sus.detalle("s", "a")

    # Suscripción sigue respondiendo: su 404 llega como RESOURCE_NOT_FOUND, no como 503 degradado.
    assert error.value.code == "RESOURCE_NOT_FOUND"
    assert sana.llamadas == 1


async def test_disponible_no_pasa_por_el_circuito(cotizacion, servicio, circuito):
    servicio.caido = True
    await _fallar(cotizacion, 3)
    servicio.caido = False

    assert circuito.estado is EstadoCircuito.ABIERTO
    assert await cotizacion.disponible()


# --- por la API real ---------------------------------------------------------------------------------------------------


def test_por_la_api_el_503_degradado_trae_retry_after_y_el_aviso_del_perfil(cliente, token, servicio, circuito, reloj):
    cliente.app.state.cotizaciones = ClienteCotizacion(
        httpx.AsyncClient(base_url="http://cotizacion", transport=httpx.MockTransport(servicio)), circuito
    )
    cabeceras = {"Authorization": f"Bearer {token()}"}
    servicio.caido = True

    def cotizar(clave):
        return cliente.post("/partners/v1/cotizaciones", json=COTIZAR, headers={**cabeceras, "X-Idempotency-Key": clave})

    respuestas = [cotizar(f"k{i}") for i in range(4)]

    assert [r.status_code for r in respuestas] == [503, 503, 503, 503]
    assert [r.json()["error"]["code"] for r in respuestas] == ["SERVICE_UNAVAILABLE"] * 3 + ["SERVICE_DEGRADED"]
    degradada = respuestas[3]
    assert degradada.headers["Retry-After"] == "30"
    assert "perfil por defecto" in degradada.json()["error"]["message"]
    assert servicio.llamadas == 3
    # Recupera: tras el enfriamiento el sondeo cierra el circuito y vuelve el 201.
    servicio.caido = False
    reloj.ahora += 31
    assert cotizar("k-ok").status_code == 201
