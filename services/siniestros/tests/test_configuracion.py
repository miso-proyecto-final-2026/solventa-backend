"""Fuera de desarrollo, una variable faltante impide arrancar (falla cerrado)."""

import asyncio

import pytest

from app.configuracion import VARIABLE_MODO_DESARROLLO, ErrorConfiguracion
from app.siniestros.config import ConfigSiniestros
from app.siniestros.wiring import construir_ensamblado


def test_sin_base_de_datos_no_arranca_fuera_de_desarrollo(monkeypatch):
    monkeypatch.delenv(VARIABLE_MODO_DESARROLLO, raising=False)

    with pytest.raises(ErrorConfiguracion, match="SINIESTROS_DATABASE_URL"):
        construir_ensamblado(ConfigSiniestros())


def test_sin_kafka_no_arranca_fuera_de_desarrollo(monkeypatch):
    monkeypatch.delenv(VARIABLE_MODO_DESARROLLO, raising=False)

    with pytest.raises(ErrorConfiguracion, match="KAFKA_BOOTSTRAP_SERVERS"):
        construir_ensamblado(ConfigSiniestros(database_url="sqlite://"))


def test_en_desarrollo_arranca_con_publicador_en_memoria_y_sin_consumidor():
    ensamblado = construir_ensamblado(ConfigSiniestros(database_url="sqlite://"))

    assert ensamblado.receptor is not None and ensamblado.consumidor is None
    ensamblado.engine.dispose()


def test_con_kafka_configurado_se_arma_el_consumidor_del_grupo_del_servicio(monkeypatch):
    monkeypatch.setenv("KAFKA_SEGURIDAD", "PLAINTEXT")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")

    async def armar():
        return construir_ensamblado(ConfigSiniestros(database_url="sqlite://", kafka_brokers="kafka:9092"))

    ensamblado = asyncio.run(armar())

    assert ensamblado.consumidor is not None
    ensamblado.engine.dispose()


def test_las_llaves_se_leen_como_fuente_igual_clave():
    config = ConfigSiniestros.desde_entorno(
        {"SINIESTROS_API_KEYS": "proveedor-sismos=k1, proveedor-vuelos=k2 ,sinnombre,=vacia"}
    )

    assert config.api_keys == {"k1": "proveedor-sismos", "k2": "proveedor-vuelos"}


def test_sin_llaves_configuradas_el_mapa_queda_vacio():
    assert ConfigSiniestros.desde_entorno({}).api_keys == {}
