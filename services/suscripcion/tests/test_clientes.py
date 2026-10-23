"""Clientes HTTP de los servicios vecinos, contra respuestas simuladas."""

import httpx
import pytest
from app.suscripcion.clientes import (
    ClienteCotizacion,
    ClienteIdentidad,
    ClientePerfilamiento,
)
from app.suscripcion.errores import DependenciaNoDisponible


def _http(manejador):
    return httpx.AsyncClient(transport=httpx.MockTransport(manejador), base_url="http://vecino")


COTIZACION = {
    "id": "cot-1", "cliente_id": "cli-1", "producto": "microseguro_vida", "moneda": "COP", "prima_anual": 175000,
    "prima_mensual": 14583, "vigente_hasta": "2026-10-30T12:00:00Z",
    "coberturas": [{"tipo": "muerte", "monto": 30000000, "deducible": 0}, {"tipo": "invalidez", "monto": 10000000, "deducible": 0}],
}


async def test_identidad_envia_la_clave_y_el_token_y_devuelve_el_cliente():
    vistos = {}

    def manejador(req):
        vistos.update(req.headers)
        return httpx.Response(200, json={"cliente_id": "cli-1"})

    assert await ClienteIdentidad(_http(manejador), "clave-s").cliente_de_token("tok") == "cli-1"
    assert vistos["x-service-key"] == "clave-s" and vistos["authorization"] == "Bearer tok"


async def test_identidad_con_token_invalido_devuelve_none():
    assert await ClienteIdentidad(_http(lambda r: httpx.Response(401)), "k").cliente_de_token("x") is None


@pytest.mark.parametrize("respuesta", [httpx.Response(500), httpx.Response(403)])
async def test_identidad_caida_o_con_clave_mala_no_se_confunde_con_token_invalido(respuesta):
    with pytest.raises(DependenciaNoDisponible):
        await ClienteIdentidad(_http(lambda r: respuesta), "k").cliente_de_token("x")


async def test_identidad_sin_red_es_dependencia_no_disponible():
    def falla(_):
        raise httpx.ConnectError("sin red")

    with pytest.raises(DependenciaNoDisponible):
        await ClienteIdentidad(_http(falla), "k").cliente_de_token("x")


async def test_cotizacion_toma_la_cobertura_principal_como_monto_asegurado_y_entiende_la_fecha():
    datos = await ClienteCotizacion(_http(lambda r: httpx.Response(200, json=COTIZACION))).obtener("cot-1")
    assert datos.monto_asegurado == 30_000_000 and datos.prima_anual == 175000
    assert datos.vigente_hasta.tzinfo is not None


async def test_cotizacion_inexistente_devuelve_none_y_un_fallo_se_propaga():
    assert await ClienteCotizacion(_http(lambda r: httpx.Response(404))).obtener("x") is None
    with pytest.raises(DependenciaNoDisponible):
        await ClienteCotizacion(_http(lambda r: httpx.Response(502))).obtener("x")


async def test_perfil_disponible_se_traduce_a_perfil_de_riesgo():
    cuerpo = {
        "completitud": 0.9, "degradado": False, "con_consentimiento": True,
        "campos": {"ratio_endeudamiento": {"valor": 0.3, "fuente": "open_finance"}, "reportes_negativos": {"valor": 0}},
    }
    perfil = await ClientePerfilamiento(_http(lambda r: httpx.Response(200, json=cuerpo)), "k").perfil_de("cli-1")
    assert perfil.disponible and perfil.campos == {"ratio_endeudamiento": 0.3, "reportes_negativos": 0}
    assert perfil.completitud == 0.9


async def test_perfil_sin_consentimiento_sin_perfil_o_con_fallo_nunca_detiene_la_compra():
    sin_consentimiento = await ClientePerfilamiento(_http(lambda r: httpx.Response(403)), "k").perfil_de("c")
    assert sin_consentimiento.disponible and not sin_consentimiento.con_consentimiento
    for estado in (404, 500):
        perfil = await ClientePerfilamiento(_http(lambda r, e=estado: httpx.Response(e)), "k").perfil_de("c")
        assert not perfil.disponible and perfil.motivo_indisponible

    def falla(_):
        raise httpx.ReadTimeout("lento")

    assert not (await ClientePerfilamiento(_http(falla), "k").perfil_de("c")).disponible
