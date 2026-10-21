"""Perfiles demo por cliente en los simuladores (HU38)."""

from pathlib import Path

import pytest

from app.proveedores.demo import VARIABLE_ARCHIVO, CatalogoPerfilesDemo
from app.proveedores.simuladores import (
    ConfigSimulador,
    SimuladorOpenData,
    SimuladorOpenFinance,
)

ARCHIVO = Path(__file__).parents[2] / "app" / "proveedores" / "perfiles_demo.yaml"
FAVORABLE = "00000000-0000-4000-8000-0000000000a1"
RAPIDO = ConfigSimulador(latencia_min_ms=0, latencia_max_ms=0)


def test_sin_archivo_configurado_no_hay_catalogo():
    assert CatalogoPerfilesDemo.desde_entorno({}) is None


def test_el_archivo_se_toma_de_la_variable_de_entorno():
    catalogo = CatalogoPerfilesDemo.desde_entorno({VARIABLE_ARCHIVO: str(ARCHIVO)})
    assert catalogo is not None
    assert catalogo.para(FAVORABLE, "open_finance")["debtRatio"] == 0.15


def test_un_cliente_no_asignado_no_tiene_perfil_demo():
    assert CatalogoPerfilesDemo.desde_archivo(ARCHIVO).para("cualquiera", "open_finance") is None


def test_un_cliente_apuntando_a_un_perfil_inexistente_es_un_error():
    with pytest.raises(ValueError, match="sin definir"):
        CatalogoPerfilesDemo({}, {"c1": "fantasma"})


async def test_el_simulador_devuelve_el_perfil_asignado_al_cliente():
    demo = CatalogoPerfilesDemo.desde_archivo(ARCHIVO)

    crudo = await SimuladorOpenFinance(RAPIDO, perfiles_demo=demo).consultar_perfil(FAVORABLE)
    abierto = await SimuladorOpenData(RAPIDO, perfiles_demo=demo).consultar_perfil(FAVORABLE)

    assert crudo["customerId"] == FAVORABLE and crudo["debtRatio"] == 0.15
    assert abierto["reportes_negativos"] == 0


async def test_sin_catalogo_o_sin_asignacion_se_mantiene_el_perfil_neutro():
    demo = CatalogoPerfilesDemo.desde_archivo(ARCHIVO)
    for sim in (SimuladorOpenFinance(RAPIDO), SimuladorOpenFinance(RAPIDO, perfiles_demo=demo)):
        assert (await sim.consultar_perfil("no-asignado"))["debtRatio"] == 0.32
