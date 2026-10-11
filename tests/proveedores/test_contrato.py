"""Contrato de los puertos verificado contra los adaptadores simulados (base de HA06)."""

import pytest

from app.proveedores.dominio import ProveedorError
from app.proveedores.simuladores import (
    ConfigSimulador,
    ModoFalla,
    SimuladorOpenData,
    SimuladorOpenFinance,
)

RAPIDO = {"latencia_min_ms": 0.0, "latencia_max_ms": 1.0}


@pytest.mark.parametrize("clase", [SimuladorOpenFinance, SimuladorOpenData])
async def test_adaptador_cumple_el_puerto(clase):
    adaptador = clase(ConfigSimulador(**RAPIDO))
    payload = await adaptador.consultar_perfil("c1")
    assert isinstance(payload, dict) and payload
    assert await adaptador.sondear() is None


@pytest.mark.parametrize("clase", [SimuladorOpenFinance, SimuladorOpenData])
async def test_simulador_vacio_y_5xx_por_configuracion(clase):
    assert (
        await clase(ConfigSimulador(modo=ModoFalla.VACIO, **RAPIDO)).consultar_perfil(
            "c"
        )
        == {}
    )
    with pytest.raises(ProveedorError):
        await clase(
            ConfigSimulador(modo=ModoFalla.ERROR_5XX, **RAPIDO)
        ).consultar_perfil("c")


def test_simulador_se_configura_por_entorno():
    cfg = ConfigSimulador.desde_entorno(
        "OPEN_FINANCE_SIM",
        {"OPEN_FINANCE_SIM_MODO": "timeout", "OPEN_FINANCE_SIM_LATENCIA_MAX_MS": "900"},
    )
    assert cfg.modo is ModoFalla.TIMEOUT and cfg.latencia_max_ms == 900.0
