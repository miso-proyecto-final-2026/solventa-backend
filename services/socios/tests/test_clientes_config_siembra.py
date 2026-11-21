"""Cliente de Cotización (integración por contrato HTTP), configuración y script de siembra."""

import json
import sys
from pathlib import Path

import httpx
import pytest
from app.socios.clientes import ClienteCotizacion
from app.socios.config import SECRETO_JWT_DESARROLLO, ConfigSocios
from app.socios.errores import (
    ErrorInterno,
    RecursoNoEncontrado,
    ServicioNoDisponible,
    SolicitudInvalida,
)
from app.socios.repositorio import buscar_cliente, contrato_vigente
from app.socios.secretos import verificar_secreto
from app.socios.wiring import construir_ensamblado

from app.configuracion import ErrorConfiguracion
from app.contexto import correlacion_id
from tests.dobles import SECRETO_PRUEBA, cotizacion_de

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import sembrar_socio_demo

# --- cliente de Cotización ------------------------------------------------------------------------------------------


def _cliente(manejador) -> ClienteCotizacion:
    return ClienteCotizacion(httpx.AsyncClient(base_url="http://cotizacion", transport=httpx.MockTransport(manejador)))


async def test_envia_la_solicitud_a_cotizacion_con_la_correlacion():
    vistas = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        cuerpo = json.loads(peticion.content)
        return httpx.Response(201, json=cotizacion_de(cuerpo["cliente_id"], cuerpo["producto"]))

    token = correlacion_id.set("corr-123")
    try:
        r = await _cliente(manejador).crear("cli-x", "microseguro_vida", {"edad": 30})
    finally:
        correlacion_id.reset(token)

    assert r["cliente_id"] == "cli-x"
    assert vistas[0].url.path == "/v1/cotizaciones"
    assert vistas[0].headers["X-Correlation-Id"] == "corr-123"
    assert json.loads(vistas[0].content) == {"cliente_id": "cli-x", "producto": "microseguro_vida", "datos": {"edad": 30}}


async def test_la_validacion_de_cotizacion_se_traduce_a_solicitud_invalida():
    def manejador(_p):
        return httpx.Response(
            422, json={"error": {"code": "VALIDATION_ERROR", "details": [{"campo": "producto", "motivo": "desconocido"}]}}
        )

    with pytest.raises(SolicitudInvalida) as error:
        await _cliente(manejador).crear("c", "x", {})

    assert error.value.detalles == [{"campo": "producto", "motivo": "desconocido"}]


async def test_un_422_sin_cuerpo_json_no_rompe():
    with pytest.raises(SolicitudInvalida) as error:
        await _cliente(lambda _p: httpx.Response(422, text="no json")).crear("c", "x", {})

    assert error.value.detalles == []


@pytest.mark.parametrize("estado", [500, 502, 503])
async def test_un_5xx_de_cotizacion_es_servicio_no_disponible(estado):
    with pytest.raises(ServicioNoDisponible):
        await _cliente(lambda _p: httpx.Response(estado)).crear("c", "x", {})


async def test_un_404_sin_codigo_conocido_es_un_error_interno_y_con_su_codigo_es_404():
    with pytest.raises(ErrorInterno):
        await _cliente(lambda _p: httpx.Response(404)).crear("c", "x", {})
    with pytest.raises(RecursoNoEncontrado):
        await _cliente(
            lambda _p: httpx.Response(404, json={"error": {"code": "COTIZACION_NO_ENCONTRADA", "message": "x"}})
        ).crear("c", "x", {})


async def test_un_error_de_red_es_servicio_no_disponible():
    def manejador(peticion):
        raise httpx.ConnectError("caído", request=peticion)

    cliente = _cliente(manejador)
    with pytest.raises(ServicioNoDisponible):
        await cliente.crear("c", "x", {})
    assert not await cliente.disponible()


async def test_disponible_consulta_la_sonda_de_cotizacion():
    assert await _cliente(lambda p: httpx.Response(200 if p.url.path == "/health" else 404)).disponible()
    assert not await _cliente(lambda _p: httpx.Response(503)).disponible()


# --- configuración --------------------------------------------------------------------------------------------------


def test_fuera_de_desarrollo_la_configuracion_es_obligatoria():
    with pytest.raises(ErrorConfiguracion):
        ConfigSocios.desde_entorno({})


def test_en_desarrollo_usa_sustitutos_locales():
    config = ConfigSocios.desde_entorno({"SOLVENTA_MODO_DESARROLLO": "1"})

    assert config.database_url.startswith("sqlite")
    assert config.redis_url is None
    assert config.jwt_secreto == SECRETO_JWT_DESARROLLO


def test_lee_la_configuracion_de_staging():
    config = ConfigSocios.desde_entorno(
        {
            "DATABASE_URL": "postgresql://u@h/db",
            "REDIS_URL": "rediss://cache:6379",
            "COTIZACION_URL": "http://solventa-backend",
            "IDENTIDAD_URL": "http://solventa-identidad",
            "IDENTIDAD_SERVICE_KEY": "clave-identidad",
            "SUSCRIPCION_URL": "http://solventa-suscripcion",
            "SUSCRIPCION_SERVICE_KEY": "clave-suscripcion",
            "SOCIOS_JWT_SECRETO": "s" * 40,
            "SOCIOS_JWT_TTL_S": "1800",
        }
    )

    assert (config.redis_url, config.cotizacion_url, config.jwt_ttl_s) == ("rediss://cache:6379", "http://solventa-backend", 1800)
    assert (config.identidad_url, config.identidad_service_key) == ("http://solventa-identidad", "clave-identidad")
    assert (config.suscripcion_url, config.suscripcion_service_key) == ("http://solventa-suscripcion", "clave-suscripcion")


@pytest.mark.parametrize("falta", ["IDENTIDAD_URL", "IDENTIDAD_SERVICE_KEY", "SUSCRIPCION_URL", "SUSCRIPCION_SERVICE_KEY"])
def test_fuera_de_desarrollo_faltan_las_variables_de_identidad_y_suscripcion(falta):
    entorno = {
        "DATABASE_URL": "postgresql://u@h/db",
        "REDIS_URL": "rediss://cache:6379",
        "COTIZACION_URL": "http://solventa-backend",
        "IDENTIDAD_URL": "http://solventa-identidad",
        "IDENTIDAD_SERVICE_KEY": "clave-identidad",
        "SUSCRIPCION_URL": "http://solventa-suscripcion",
        "SUSCRIPCION_SERVICE_KEY": "clave-suscripcion",
        "SOCIOS_JWT_SECRETO": "s" * 40,
    }
    del entorno[falta]

    with pytest.raises(ErrorConfiguracion, match=falta):
        ConfigSocios.desde_entorno(entorno)


def test_un_secreto_jwt_corto_impide_arrancar():
    with pytest.raises(ErrorConfiguracion, match="al menos"):
        ConfigSocios.desde_entorno({"SOLVENTA_MODO_DESARROLLO": "1", "SOCIOS_JWT_SECRETO": "corto"})


async def test_el_ensamblado_local_arranca_y_se_detiene(tmp_path):
    config = ConfigSocios.desde_entorno(
        {"SOLVENTA_MODO_DESARROLLO": "1", "DATABASE_URL": f"sqlite:///{tmp_path / 's.db'}"}
    )

    ensamblado = construir_ensamblado(config)

    assert await ensamblado.registro.bd_disponible()
    assert await ensamblado.almacen.disponible()
    await ensamblado.detener()


async def test_con_redis_url_usa_redis(tmp_path):
    config = ConfigSocios.desde_entorno(
        {"SOLVENTA_MODO_DESARROLLO": "1", "DATABASE_URL": f"sqlite:///{tmp_path / 's.db'}", "REDIS_URL": "redis://localhost:1"}
    )

    ensamblado = construir_ensamblado(config)

    assert type(ensamblado.almacen).__name__ == "AlmacenRedis"
    await ensamblado.detener()


# --- siembra --------------------------------------------------------------------------------------------------------


def test_sembrar_es_idempotente_y_guarda_solo_el_hash(fabrica):
    for cuota in (5, 7):
        sembrar_socio_demo.sembrar(
            fabrica, client_id="demo", client_secret=SECRETO_PRUEBA, nombre="Demo", cuota_rps=cuota,
            endpoints=["cotizaciones:crear"], rondas=4,
        )

    from datetime import UTC, datetime

    with fabrica() as sesion:
        cliente = buscar_cliente(sesion, "demo")
        contrato = contrato_vigente(sesion, "demo", "v1", datetime.now(UTC))
        assert SECRETO_PRUEBA not in cliente.client_secret_hash
        assert verificar_secreto(SECRETO_PRUEBA, cliente.client_secret_hash)
        assert contrato.cuota_rps == 7
        assert contrato.endpoints_habilitados == ["cotizaciones:crear"]


@pytest.mark.parametrize(
    "opciones",
    [{"endpoints": ["borrar:todo"]}, {"cuota_rps": 0}, {"client_secret": "corto"}],
)
def test_sembrar_valida_la_entrada(fabrica, opciones):
    base = {"client_id": "demo", "client_secret": SECRETO_PRUEBA, "nombre": "Demo", "cuota_rps": 5, "endpoints": ["cotizaciones:crear"], "rondas": 4}

    with pytest.raises(ValueError):
        sembrar_socio_demo.sembrar(fabrica, **{**base, **opciones})


def test_main_sin_secreto_no_siembra(capsys):
    assert sembrar_socio_demo.main({"SOLVENTA_MODO_DESARROLLO": "1"}) == 2


def test_main_fuera_de_desarrollo_exige_la_base(capsys):
    assert sembrar_socio_demo.main({"SOCIO_DEMO_CLIENT_SECRET": SECRETO_PRUEBA}) == 2


def test_main_siembra_sin_imprimir_el_secreto(tmp_path, capsys):
    env = {
        "DATABASE_URL": f"sqlite:///{tmp_path / 's.db'}",
        "SOCIO_DEMO_CLIENT_SECRET": SECRETO_PRUEBA,
        "SOCIO_DEMO_CUOTA_RPS": "3",
    }

    assert sembrar_socio_demo.main(env) == 0
    assert sembrar_socio_demo.main({**env, "SOCIO_DEMO_CUOTA_RPS": "0"}) == 2

    salida = capsys.readouterr()
    assert "socio-demo" in salida.out
    assert SECRETO_PRUEBA not in salida.out + salida.err
