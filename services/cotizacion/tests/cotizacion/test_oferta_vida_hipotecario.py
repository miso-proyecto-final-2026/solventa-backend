"""Oferta de vida hipotecario para el asesor (HU26): acceso, fachada hacia Perfilamiento, registro
como cotización y errores."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.acceso import VerificadorJwtHs256
from app.contexto import correlacion_id
from app.cotizacion.api_asesor import router as router_asesor
from app.cotizacion.errores import (
    OfertaNoDisponible,
    OfertaSinConsentimiento,
    OfertaSinPerfil,
    SumaAseguradaFueraDeRango,
)
from app.cotizacion.oferta import (
    OfertaHttp,
    OfertaPerfilamiento,
    OfertaSinConfigurar,
    construir_oferta,
)

from .test_asesor import ASESOR_ID, EMISOR, SECRETO, _auth

CLIENTE = "cli-1"
RUTA = f"/v1/asesor/clientes/{CLIENTE}/ofertas-vida-hipotecario"

# Forma de la respuesta de HU25 (`GET /v1/perfilamiento/{id}/oferta`); cifras de ejemplo, sin respaldo actuarial.
OFERTA = {
    "oferta_id": "0f6c2a9e-7d41-4b8a-9e1f-3a2b4c5d6e7f",
    "cliente_id": CLIENTE,
    "producto": "vida_hipotecario",
    "moneda": "COP",
    "suma_asegurada": 200_000_000,
    "prima_anual": 360_000,
    "prima_mensual": 30_000,
    "etapa_de_vida": "desarrollo",
    "version_reglas": "2026.11.1-prototipo",
    "perfil_id": "6a1f0c4e-2b7d-4e9a-8c3f-1d2e3f4a5b6c",
    "perfil_creado_en": "2026-11-20T15:00:00+00:00",
    "degradado": False,
    "desglose": [
        {
            "paso": "prima_base",
            "nombre": "0.0018 sobre la suma asegurada",
            "factor": None,
            "valor_perfil": None,
            "ajuste": 360_000,
            "acumulado": 360_000,
            "sin_dato": False,
        },
        {
            "paso": "senal",
            "nombre": "ingresos_mensuales_cop",
            "factor": "1.00",
            "valor_perfil": 4_500_000,
            "ajuste": 0,
            "acumulado": 360_000,
            "sin_dato": False,
        },
    ],
    "coberturas": [
        {"tipo": "muerte", "monto_cubierto": 200_000_000, "deducible_porcentaje": "0"},
        {"tipo": "incapacidad_total_permanente", "monto_cubierto": 200_000_000, "deducible_porcentaje": "0.05"},
    ],
    "creado_en": "2026-11-20T15:00:01+00:00",
}


class OfertasFalsas:
    """Responde OFERTA o lanza los errores pedidos, y anota cada llamada."""

    def __init__(
        self,
        cuerpo: dict | None = None,
        error_enriquecer: Exception | None = None,
        error_calcular: Exception | None = None,
    ) -> None:
        self.cuerpo = OFERTA if cuerpo is None else cuerpo
        self.error_enriquecer = error_enriquecer
        self.error_calcular = error_calcular
        self.llamadas: list[tuple] = []

    async def enriquecer(self, cliente_id, asesor_id):
        self.llamadas.append(("enriquecer", cliente_id, asesor_id))
        if self.error_enriquecer:
            raise self.error_enriquecer

    async def calcular(self, cliente_id, asesor_id, suma_asegurada):
        self.llamadas.append(("calcular", cliente_id, asesor_id, suma_asegurada))
        if self.error_calcular:
            raise self.error_calcular
        return OfertaPerfilamiento.model_validate(self.cuerpo)

    async def cerrar(self):
        return None


@pytest.fixture
def app_oferta(entorno):
    def _crear(ofertas):
        e = entorno()
        app = e.client.app
        app.include_router(router_asesor)
        app.state.verificador_asesor = VerificadorJwtHs256(SECRETO, EMISOR)
        app.state.perfiles = e.perfiles
        app.state.ofertas = ofertas
        return e

    return _crear


def _cotizaciones_guardadas(e) -> int:
    from sqlalchemy import func, select

    from app.cotizacion.modelos_db import CotizacionDB

    with e.engine.connect() as c:
        return c.execute(select(func.count()).select_from(CotizacionDB)).scalar_one()


# --- acceso -----------------------------------------------------------------


def test_sin_token_401_y_con_otro_rol_403_sin_llamar_a_perfilamiento(app_oferta):
    ofertas = OfertasFalsas()
    cliente = app_oferta(ofertas).client

    assert cliente.post(RUTA, json={}).status_code == 401
    r = cliente.post(RUTA, json={}, headers=_auth(rol="CLIENTE"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "SIN_PERMISO"
    assert ofertas.llamadas == []


# --- caso feliz ---------------------------------------------------------------


def test_al_abrir_el_flujo_se_enriquece_el_perfil_y_luego_se_calcula_la_oferta(app_oferta):
    ofertas = OfertasFalsas()

    r = app_oferta(ofertas).client.post(RUTA, json={}, headers=_auth())

    assert r.status_code == 201
    assert ofertas.llamadas == [
        ("enriquecer", CLIENTE, ASESOR_ID),
        ("calcular", CLIENTE, ASESOR_ID, None),
    ]


def test_la_oferta_trae_la_prima_de_perfilamiento_y_queda_como_cotizacion(app_oferta):
    e = app_oferta(OfertasFalsas())

    o = e.client.post(RUTA, json={}, headers=_auth()).json()

    assert (o["producto"], o["prima_anual"], o["prima_mensual"]) == ("vida_hipotecario", 360_000, 30_000)
    assert (o["suma_asegurada"], o["etapa_de_vida"], o["degradado"]) == (200_000_000, "desarrollo", False)
    assert o["oferta_id"] == OFERTA["oferta_id"] and o["perfil_id"] == OFERTA["perfil_id"]
    assert (o["asesor_id"], o["canal"]) == (ASESOR_ID, "WEB_ASESOR")
    # El deducible pasa de porcentaje (Perfilamiento) a monto (Cotización y Suscripción).
    assert o["coberturas"] == [
        {"tipo": "muerte", "monto": 200_000_000, "deducible": 0},
        {"tipo": "incapacidad_total_permanente", "monto": 200_000_000, "deducible": 10_000_000},
    ]
    assert _cotizaciones_guardadas(e) == 1


def test_al_asesor_no_le_llega_ninguna_cifra_del_cliente(app_oferta):
    r = app_oferta(OfertasFalsas()).client.post(RUTA, json={}, headers=_auth())

    assert [a["paso"] for a in r.json()["ajustes"]] == ["prima_base", "senal"]
    assert "valor_perfil" not in r.text and "4500000" not in r.text


def test_suscripcion_lee_la_cotizacion_registrada_con_los_datos_de_la_oferta(app_oferta):
    """Lo que lee `ClienteCotizacion` de Suscripción (HU39): mismo producto, primas, coberturas y asesor."""
    e = app_oferta(OfertasFalsas())
    o = e.client.post(RUTA, json={}, headers=_auth()).json()

    c = e.client.get(f"/v1/cotizaciones/{o['cotizacion_id']}").json()

    assert (c["cliente_id"], c["producto"], c["moneda"]) == (CLIENTE, "vida_hipotecario", "COP")
    assert (int(c["prima_anual"]), int(c["prima_mensual"])) == (360_000, 30_000)
    assert max(int(x["monto"]) for x in c["coberturas"]) == 200_000_000
    assert [x["deducible"] for x in c["coberturas"]] == [0, 10_000_000]
    assert c["asesor_id"] == ASESOR_ID and c["sin_cuenta"] is False
    assert c["version_reglas"] == "2026.11.1-prototipo"
    assert c["vigente_hasta"] == o["vigente_hasta"]


def test_los_datos_guardados_llevan_a_la_oferta_y_al_perfil_en_perfilamiento(app_oferta):
    from sqlalchemy import select

    from app.cotizacion.modelos_db import CotizacionDB

    e = app_oferta(OfertasFalsas())
    o = e.client.post(RUTA, json={}, headers=_auth()).json()

    with e.engine.connect() as c:
        datos = c.execute(select(CotizacionDB.datos).where(CotizacionDB.id == o["cotizacion_id"])).scalar_one()
    if isinstance(datos, str):
        datos = json.loads(datos)
    assert datos == {
        "suma_asegurada": 200_000_000,
        "etapa_de_vida": "desarrollo",
        "oferta_id": OFERTA["oferta_id"],
        "perfil_id": OFERTA["perfil_id"],
        "precio_calculado_por": "perfilamiento",
    }


def test_al_cambiar_la_suma_no_se_vuelve_a_enriquecer(app_oferta):
    ofertas = OfertasFalsas()

    r = app_oferta(ofertas).client.post(
        RUTA, json={"suma_asegurada": 150_000_000, "enriquecer": False}, headers=_auth()
    )

    assert r.status_code == 201
    assert ofertas.llamadas == [("calcular", CLIENTE, ASESOR_ID, 150_000_000)]


def test_cada_oferta_es_una_cotizacion_nueva(app_oferta):
    e = app_oferta(OfertasFalsas())

    a = e.client.post(RUTA, json={}, headers=_auth()).json()["cotizacion_id"]
    b = e.client.post(RUTA, json={"enriquecer": False}, headers=_auth()).json()["cotizacion_id"]

    assert a != b and _cotizaciones_guardadas(e) == 2


# --- errores ------------------------------------------------------------------


def test_sin_consentimiento_responde_409_y_no_registra_nada(app_oferta):
    e = app_oferta(OfertasFalsas(error_enriquecer=OfertaSinConsentimiento()))

    r = e.client.post(RUTA, json={}, headers=_auth())

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONSENTIMIENTO_REQUERIDO"
    assert _cotizaciones_guardadas(e) == 0


def test_sin_perfil_y_sin_enriquecer_responde_404(app_oferta):
    e = app_oferta(OfertasFalsas(error_calcular=OfertaSinPerfil()))

    r = e.client.post(RUTA, json={"enriquecer": False}, headers=_auth())

    assert r.status_code == 404 and r.json()["error"]["code"] == "PERFIL_NO_ENCONTRADO"


@pytest.mark.parametrize("donde", ["error_enriquecer", "error_calcular"])
def test_perfilamiento_caido_responde_503_y_no_registra_nada(app_oferta, donde):
    e = app_oferta(OfertasFalsas(**{donde: OfertaNoDisponible()}))

    r = e.client.post(RUTA, json={}, headers=_auth())

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "OFERTA_NO_DISPONIBLE"
    assert set(r.json()["error"]) == {"code", "message", "details", "trace_id"}
    assert _cotizaciones_guardadas(e) == 0


def test_suma_fuera_de_rango_responde_422_con_el_motivo_de_perfilamiento(app_oferta):
    motivo = "debe estar entre 20000000 y 800000000"
    e = app_oferta(OfertasFalsas(error_calcular=SumaAseguradaFueraDeRango(motivo)))

    r = e.client.post(RUTA, json={"suma_asegurada": 5_000_000}, headers=_auth())

    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"campo": "suma_asegurada", "motivo": motivo}]
    assert _cotizaciones_guardadas(e) == 0


@pytest.mark.parametrize("suma", [0, -1, "mucho"])
def test_una_suma_invalida_se_rechaza_sin_llamar_a_perfilamiento(app_oferta, suma):
    ofertas = OfertasFalsas()

    r = app_oferta(ofertas).client.post(RUTA, json={"suma_asegurada": suma}, headers=_auth())

    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert ofertas.llamadas == []


# --- adaptador HTTP -----------------------------------------------------------


def _adaptador(manejador) -> OfertaHttp:
    return OfertaHttp(
        base_url="http://perfilamiento",
        api_key="clave-de-cotizacion",
        cliente=httpx.AsyncClient(base_url="http://perfilamiento", transport=httpx.MockTransport(manejador)),
    )


def _error(estado: int, code: str, details: list | None = None) -> httpx.Response:
    return httpx.Response(estado, json={"error": {"code": code, "message": "m", "details": details or []}})


async def test_el_adaptador_envia_la_clave_propia_el_asesor_y_la_correlacion():
    vistas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        if request.url.path.endswith("/enriquecer"):
            return httpx.Response(201, json={"perfil_id": "p"})
        return httpx.Response(200, json=OFERTA)

    token = correlacion_id.set("corr-1")
    try:
        adaptador = _adaptador(manejador)
        await adaptador.enriquecer(CLIENTE, ASESOR_ID)
        oferta = await adaptador.calcular(CLIENTE, ASESOR_ID, 150_000_000)
    finally:
        correlacion_id.reset(token)

    assert [(r.method, r.url.path) for r in vistas] == [
        ("POST", f"/v1/perfilamiento/{CLIENTE}/enriquecer"),
        ("GET", f"/v1/perfilamiento/{CLIENTE}/oferta"),
    ]
    assert vistas[1].url.params["suma_asegurada"] == "150000000"
    for r in vistas:
        assert r.headers["X-Service-Key"] == "clave-de-cotizacion"
        assert r.headers["X-Consultado-Por"] == ASESOR_ID
        assert r.headers["X-Correlation-Id"] == "corr-1"
    assert oferta.prima_anual == 360_000


async def test_el_adaptador_sin_suma_no_envia_el_parametro():
    vistas: list[httpx.Request] = []

    def manejador(request):
        vistas.append(request)
        return httpx.Response(200, json=OFERTA)

    await _adaptador(manejador).calcular(CLIENTE, ASESOR_ID, None)

    assert "suma_asegurada" not in vistas[0].url.params


@pytest.mark.parametrize("metodo", ["enriquecer", "calcular"])
async def test_el_adaptador_traduce_consentimiento_no_vigente(metodo):
    adaptador = _adaptador(lambda r: _error(403, "CONSENTIMIENTO_NO_VIGENTE"))

    with pytest.raises(OfertaSinConsentimiento):
        if metodo == "enriquecer":
            await adaptador.enriquecer(CLIENTE, ASESOR_ID)
        else:
            await adaptador.calcular(CLIENTE, ASESOR_ID, None)


@pytest.mark.parametrize(
    "respuesta",
    [
        _error(401, "NO_AUTENTICADO"),
        _error(403, "SIN_PERMISO"),
        httpx.Response(403, text="prohibido"),
    ],
)
async def test_el_adaptador_trata_una_clave_rechazada_como_no_disponible_y_lo_registra(respuesta, caplog):
    adaptador = _adaptador(lambda r: respuesta)

    with caplog.at_level(logging.ERROR, logger="solventa.oferta"), pytest.raises(OfertaNoDisponible):
        await adaptador.calcular(CLIENTE, ASESOR_ID, None)
    assert "perfilamiento_rechazo_la_clave" in caplog.text


async def test_el_adaptador_traduce_404_a_sin_perfil():
    with pytest.raises(OfertaSinPerfil):
        await _adaptador(lambda r: _error(404, "PERFIL_NO_ENCONTRADO")).calcular(CLIENTE, ASESOR_ID, None)


async def test_el_adaptador_traduce_422_a_suma_fuera_de_rango_con_el_motivo():
    detalles = [{"campo": "suma_asegurada", "motivo": "debe estar entre 20000000 y 800000000"}]
    adaptador = _adaptador(lambda r: _error(422, "VALIDATION_ERROR", detalles))

    with pytest.raises(SumaAseguradaFueraDeRango) as exc:
        await adaptador.calcular(CLIENTE, ASESOR_ID, 1)

    assert exc.value.detalles[0].motivo == "debe estar entre 20000000 y 800000000"


async def test_un_422_sin_motivo_usa_un_motivo_generico():
    with pytest.raises(SumaAseguradaFueraDeRango) as exc:
        await _adaptador(lambda r: httpx.Response(422, json={})).calcular(CLIENTE, ASESOR_ID, 1)

    assert "límites" in exc.value.detalles[0].motivo


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(500, json={}),
        httpx.Response(200, text="no es json"),
        httpx.Response(200, json={"oferta_id": "x"}),
        httpx.Response(200, json={**OFERTA, "producto": "microseguro_vida"}),
        httpx.Response(200, json={**OFERTA, "cliente_id": "otro"}),
    ],
)
async def test_el_adaptador_rechaza_lo_que_no_es_la_oferta_esperada(respuesta):
    with pytest.raises(OfertaNoDisponible):
        await _adaptador(lambda r: respuesta).calcular(CLIENTE, ASESOR_ID, None)


@pytest.mark.parametrize("estado", [404, 500, 503])
async def test_si_enriquecer_falla_la_oferta_no_esta_disponible(estado):
    with pytest.raises(OfertaNoDisponible):
        await _adaptador(lambda r: httpx.Response(estado, json={})).enriquecer(CLIENTE, ASESOR_ID)


async def test_la_red_caida_es_no_disponible():
    def manejador(request):
        raise httpx.ConnectError("sin red", request=request)

    with pytest.raises(OfertaNoDisponible):
        await _adaptador(manejador).enriquecer(CLIENTE, ASESOR_ID)


async def test_el_cliente_id_va_escapado_en_la_ruta():
    vistas: list[httpx.Request] = []

    def manejador(request):
        vistas.append(request)
        return httpx.Response(201, json={})

    await _adaptador(manejador).enriquecer("a/b", ASESOR_ID)

    assert vistas[0].url.raw_path.decode().startswith("/v1/perfilamiento/a%2Fb/")


# --- configuración ------------------------------------------------------------


async def test_sin_configuracion_avisa_en_el_log_y_la_oferta_no_esta_disponible(caplog):
    with caplog.at_level(logging.WARNING, logger="solventa.oferta"):
        ofertas = construir_oferta({"PERFILAMIENTO_URL": "http://perfilamiento"})

    assert isinstance(ofertas, OfertaSinConfigurar)
    assert "PERFILAMIENTO_SERVICE_KEY" in caplog.text
    with pytest.raises(OfertaNoDisponible):
        await ofertas.enriquecer(CLIENTE, ASESOR_ID)
    with pytest.raises(OfertaNoDisponible):
        await ofertas.calcular(CLIENTE, ASESOR_ID, None)


def test_sin_configuracion_el_endpoint_responde_503(app_oferta):
    r = app_oferta(OfertaSinConfigurar()).client.post(RUTA, json={}, headers=_auth())

    assert r.status_code == 503 and r.json()["error"]["code"] == "OFERTA_NO_DISPONIBLE"


def test_con_url_y_clave_se_usa_el_adaptador_http():
    ofertas = construir_oferta(
        {"PERFILAMIENTO_URL": "http://perfilamiento", "PERFILAMIENTO_SERVICE_KEY": "clave"}
    )

    assert isinstance(ofertas, OfertaHttp)


# --- contrato (docs/api/cotizacion.openapi.yaml) -------------------------------

RUTA_CONTRATO = "/v1/asesor/clientes/{cliente_id}/ofertas-vida-hipotecario"


def test_el_ejemplo_y_la_respuesta_real_cumplen_el_esquema_del_contrato(app_oferta):
    from .test_contrato import _ejemplos_de_respuesta, _esquema_respuesta, _validador

    validador = _validador(_esquema_respuesta(RUTA_CONTRATO, "post", "201"))
    validador.validate(_ejemplos_de_respuesta(RUTA_CONTRATO, "post", "201")["example"])
    validador.validate(app_oferta(OfertasFalsas()).client.post(RUTA, json={}, headers=_auth()).json())


@pytest.mark.parametrize(
    ("codigo", "falsas", "cuerpo"),
    [
        ("404", {"error_calcular": OfertaSinPerfil()}, {"enriquecer": False}),
        ("409", {"error_enriquecer": OfertaSinConsentimiento()}, {}),
        ("422", {"error_calcular": SumaAseguradaFueraDeRango("debe estar entre 20000000 y 800000000")}, {}),
        ("503", {"error_calcular": OfertaNoDisponible()}, {}),
    ],
)
def test_los_errores_reales_coinciden_con_los_ejemplos_del_contrato(app_oferta, codigo, falsas, cuerpo):
    from .test_contrato import _ejemplos_de_respuesta, _esquema_respuesta, _validador

    real = app_oferta(OfertasFalsas(**falsas)).client.post(RUTA, json=cuerpo, headers=_auth()).json()
    ejemplo = _ejemplos_de_respuesta(RUTA_CONTRATO, "post", codigo)["example"]

    _validador(_esquema_respuesta(RUTA_CONTRATO, "post", codigo)).validate(real)
    assert (real["error"]["code"], real["error"]["message"], real["error"]["details"]) == (
        ejemplo["error"]["code"],
        ejemplo["error"]["message"],
        ejemplo["error"]["details"],
    )
