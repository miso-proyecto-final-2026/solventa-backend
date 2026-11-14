"""Linaje del perfil para el asesor (HU24): acceso, fachada hacia Perfilamiento y errores."""

from __future__ import annotations

import httpx
import pytest

from app.acceso import VerificadorJwtHs256
from app.configuracion import ErrorConfiguracion
from app.contexto import correlacion_id
from app.cotizacion.api_asesor import router as router_asesor
from app.cotizacion.errores import LinajeNoDisponible, LinajeNoEncontrado
from app.cotizacion.linaje import LinajeHttp, LinajeSinConfigurar, construir_linaje

from .test_asesor import ASESOR_ID, EMISOR, SECRETO, _auth

CLIENTE = "cli-1"
RUTA = f"/v1/asesor/clientes/{CLIENTE}/linaje"

LINAJE = {
    "perfil_id": "6a1f0c4e-2b7d-4e9a-8c3f-1d2e3f4a5b6c",
    "cliente_id": CLIENTE,
    "creado_en": "2026-11-14T15:00:00+00:00",
    "consentimiento_id": "cons-1",
    "fuentes": [
        {
            "fuente": "open_finance",
            "tipo_dato": "FINANCIERO",
            "campos": ["ingresos_mensuales_cop", "score_crediticio"],
            "obtenido_en": "2026-11-14T15:00:00+00:00",
            "ttl_s": 3600,
            "hit_cache": False,
            "degradado": False,
            "por_defecto": False,
            "estado_consentimiento": "OTORGADO",
        },
        {
            "fuente": "open_data",
            "tipo_dato": "SOCIOECONOMICO",
            "campos": [],
            "obtenido_en": None,
            "ttl_s": 86400,
            "hit_cache": False,
            "degradado": True,
            "por_defecto": True,
            "estado_consentimiento": "OTORGADO",
        },
    ],
    "ausentes": {"estrato": "sin_datos"},
    "conflictos": [],
}


class LinajeFalso:
    """Responde `cuerpo` o lanza `error`, y anota con qué asesor se consultó."""

    def __init__(self, cuerpo=None, error: Exception | None = None) -> None:
        self.cuerpo = LINAJE if cuerpo is None else cuerpo
        self.error = error
        self.llamadas: list[tuple[str, str]] = []

    async def obtener(self, cliente_id, asesor_id):
        self.llamadas.append((cliente_id, asesor_id))
        if self.error:
            raise self.error
        return self.cuerpo

    async def cerrar(self):
        return None


@pytest.fixture
def app_linaje(entorno):
    def _crear(linajes):
        e = entorno()
        app = e.client.app
        app.include_router(router_asesor)
        app.state.verificador_asesor = VerificadorJwtHs256(SECRETO, EMISOR)
        app.state.perfiles = e.perfiles
        app.state.linajes = linajes
        return e.client

    return _crear


# --- acceso -----------------------------------------------------------------


def test_sin_token_401_y_con_otro_rol_403_sin_llamar_a_perfilamiento(app_linaje):
    linajes = LinajeFalso()
    cliente = app_linaje(linajes)

    assert cliente.get(RUTA).status_code == 401
    r = cliente.get(RUTA, headers=_auth(rol="CLIENTE"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "SIN_PERMISO"
    assert linajes.llamadas == []


# --- fachada ----------------------------------------------------------------


def test_el_asesor_recibe_el_linaje_y_perfilamiento_sabe_a_nombre_de_quien(app_linaje):
    linajes = LinajeFalso()

    r = app_linaje(linajes).get(RUTA, headers=_auth())

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["perfil_id"] == LINAJE["perfil_id"]
    assert [f["tipo_dato"] for f in cuerpo["fuentes"]] == ["FINANCIERO", "SOCIOECONOMICO"]
    assert cuerpo["fuentes"][1]["por_defecto"] is True
    assert cuerpo["ausentes"] == {"estrato": "sin_datos"}
    assert linajes.llamadas == [(CLIENTE, ASESOR_ID)]


def test_lo_que_no_esta_en_el_contrato_no_se_reenvia(app_linaje):
    con_extra = {**LINAJE, "ingresos_mensuales_cop": 4_500_000}

    r = app_linaje(LinajeFalso(con_extra)).get(RUTA, headers=_auth())

    assert r.status_code == 200 and "ingresos_mensuales_cop" not in r.json()


def test_sin_perfil_enriquecido_responde_404(app_linaje):
    r = app_linaje(LinajeFalso(error=LinajeNoEncontrado())).get(RUTA, headers=_auth())

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PERFIL_NO_ENCONTRADO"


def test_perfilamiento_caido_responde_503_con_el_formato_estandar(app_linaje):
    r = app_linaje(LinajeFalso(error=LinajeNoDisponible())).get(RUTA, headers=_auth())

    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "LINAJE_NO_DISPONIBLE" and error["trace_id"]


def test_una_respuesta_que_no_cumple_el_contrato_responde_503(app_linaje):
    r = app_linaje(LinajeFalso({"perfil_id": "x"})).get(RUTA, headers=_auth())

    assert r.status_code == 503


# --- adaptador HTTP ----------------------------------------------------------


def _adaptador(manejador):
    transporte = httpx.MockTransport(manejador)
    return LinajeHttp(
        "http://perfilamiento",
        "clave-cotizacion",
        cliente=httpx.AsyncClient(base_url="http://perfilamiento", transport=transporte),
    )


async def test_el_adaptador_envia_la_clave_propia_el_asesor_y_la_correlacion():
    vistas: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        return httpx.Response(200, json=LINAJE)

    token = correlacion_id.set("corr-123")
    try:
        cuerpo = await _adaptador(manejador).obtener("cli/1", ASESOR_ID)
    finally:
        correlacion_id.reset(token)

    assert cuerpo == LINAJE
    (peticion,) = vistas
    assert peticion.url.raw_path == b"/v1/perfilamiento/cli%2F1/linaje"
    assert peticion.headers["X-Service-Key"] == "clave-cotizacion"
    assert peticion.headers["X-Consultado-Por"] == ASESOR_ID
    assert peticion.headers["X-Correlation-Id"] == "corr-123"


async def test_el_adaptador_traduce_404_a_linaje_no_encontrado():
    with pytest.raises(LinajeNoEncontrado):
        await _adaptador(lambda _p: httpx.Response(404)).obtener(CLIENTE, ASESOR_ID)


@pytest.mark.parametrize("estado", [401, 403, 500, 503, 422])
async def test_el_adaptador_traduce_los_demas_estados_a_no_disponible(estado):
    with pytest.raises(LinajeNoDisponible):
        await _adaptador(lambda _p: httpx.Response(estado)).obtener(CLIENTE, ASESOR_ID)


async def test_el_adaptador_traduce_la_red_caida_y_el_cuerpo_invalido_a_no_disponible():
    def caido(peticion):
        raise httpx.ConnectError("sin red", request=peticion)

    for manejador in (
        caido,
        lambda _p: httpx.Response(200, text="no es json"),
        lambda _p: httpx.Response(200, json=["no", "es", "objeto"]),
    ):
        with pytest.raises(LinajeNoDisponible):
            await _adaptador(manejador).obtener(CLIENTE, ASESOR_ID)


# --- configuración -------------------------------------------------------------


def test_sin_url_de_perfilamiento_no_arranca_fuera_de_desarrollo():
    with pytest.raises(ErrorConfiguracion):
        construir_linaje({})


async def test_sin_url_en_desarrollo_el_linaje_responde_no_disponible():
    linajes = construir_linaje({"SOLVENTA_MODO_DESARROLLO": "1"})

    assert isinstance(linajes, LinajeSinConfigurar)
    with pytest.raises(LinajeNoDisponible):
        await linajes.obtener(CLIENTE, ASESOR_ID)


def test_con_url_se_usa_el_adaptador_http_con_la_clave_propia():
    linajes = construir_linaje(
        {"PERFILAMIENTO_URL": "http://solventa-perfilamiento", "PERFILAMIENTO_SERVICE_KEY": "k"}
    )

    assert isinstance(linajes, LinajeHttp)


# --- contrato (docs/api/cotizacion.openapi.yaml) -------------------------------

RUTA_CONTRATO = "/v1/asesor/clientes/{cliente_id}/linaje"


def test_el_ejemplo_y_la_respuesta_real_cumplen_el_esquema_del_contrato(app_linaje):
    from .test_contrato import _ejemplos_de_respuesta, _esquema_respuesta, _validador

    validador = _validador(_esquema_respuesta(RUTA_CONTRATO, "get", "200"))
    validador.validate(_ejemplos_de_respuesta(RUTA_CONTRATO, "get", "200")["example"])
    validador.validate(app_linaje(LinajeFalso()).get(RUTA, headers=_auth()).json())


@pytest.mark.parametrize(
    ("codigo", "error"), [("404", LinajeNoEncontrado()), ("503", LinajeNoDisponible())]
)
def test_los_errores_reales_coinciden_con_los_ejemplos_del_contrato(app_linaje, codigo, error):
    from .test_contrato import _ejemplos_de_respuesta, _esquema_respuesta, _validador

    real = app_linaje(LinajeFalso(error=error)).get(RUTA, headers=_auth()).json()
    ejemplo = _ejemplos_de_respuesta(RUTA_CONTRATO, "get", codigo)["example"]

    _validador(_esquema_respuesta(RUTA_CONTRATO, "get", codigo)).validate(real)
    assert (real["error"]["code"], real["error"]["message"]) == (
        ejemplo["error"]["code"],
        ejemplo["error"]["message"],
    )
