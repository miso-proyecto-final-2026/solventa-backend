"""Fuera de desarrollo, una variable faltante impide arrancar (falla cerrado)."""

import pytest

from app.configuracion import VARIABLE_MODO_DESARROLLO, ErrorConfiguracion
from app.siniestros.config import ConfigSiniestros
from app.siniestros.wiring import construir_receptor


def test_sin_base_de_datos_no_arranca_fuera_de_desarrollo(monkeypatch):
    monkeypatch.delenv(VARIABLE_MODO_DESARROLLO, raising=False)

    with pytest.raises(ErrorConfiguracion, match="SINIESTROS_DATABASE_URL"):
        construir_receptor(ConfigSiniestros())


def test_en_desarrollo_arranca_con_el_sustituto_local():
    receptor, consumidor, engine = construir_receptor(ConfigSiniestros(database_url="sqlite://"))

    assert receptor is not None and consumidor is not None
    engine.dispose()


def test_las_llaves_se_leen_como_fuente_igual_clave():
    config = ConfigSiniestros.desde_entorno(
        {"SINIESTROS_API_KEYS": "proveedor-sismos=k1, proveedor-vuelos=k2 ,sinnombre,=vacia"}
    )

    assert config.api_keys == {"k1": "proveedor-sismos", "k2": "proveedor-vuelos"}


def test_sin_llaves_configuradas_el_mapa_queda_vacio():
    assert ConfigSiniestros.desde_entorno({}).api_keys == {}


def test_los_valores_numericos_invalidos_usan_el_valor_por_defecto():
    config = ConfigSiniestros.desde_entorno({"SINIESTROS_PARTICIONES": "muchas"})

    assert config.particiones == 6
