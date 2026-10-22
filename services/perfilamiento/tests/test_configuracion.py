"""Fuera de desarrollo, una variable faltante impide arrancar (falla cerrado)."""

import pytest

from app.configuracion import VARIABLE_MODO_DESARROLLO, ErrorConfiguracion
from app.perfilamiento.config import ConfigPerfilamiento
from app.perfilamiento.wiring import construir_enriquecedor, construir_fuentes
from app.proveedores.config import ConfigProveedores

VARIABLES = ("REDIS_URL", "IDENTIDAD_URL", "PERFILAMIENTO_DATABASE_URL", "DATABASE_URL")


@pytest.fixture
def produccion(monkeypatch):
    monkeypatch.delenv(VARIABLE_MODO_DESARROLLO, raising=False)
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_sin_redis_ni_identidad_no_arranca_fuera_de_desarrollo(produccion):
    with pytest.raises(ErrorConfiguracion, match="REDIS_URL"):
        construir_fuentes()


def test_sin_identidad_no_arranca_aunque_haya_redis(produccion):
    with pytest.raises(ErrorConfiguracion, match="IDENTIDAD_URL"):
        construir_fuentes(ConfigProveedores(redis_url="redis://localhost:6379/0"))


def test_sin_base_de_datos_no_arranca_fuera_de_desarrollo(produccion):
    with pytest.raises(ErrorConfiguracion, match="PERFILAMIENTO_DATABASE_URL"):
        construir_enriquecedor(object(), ConfigPerfilamiento())  # type: ignore[arg-type]


def test_en_desarrollo_arranca_con_los_sustitutos_locales(monkeypatch):
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    fuentes = construir_fuentes()
    enriquecedor, engine = construir_enriquecedor(
        fuentes, ConfigPerfilamiento(database_url="sqlite://")
    )

    assert enriquecedor is not None
    engine.dispose()


def test_las_claves_de_servicio_se_leen_con_su_nombre():
    config = ConfigPerfilamiento.desde_entorno(
        {"PERFILAMIENTO_API_KEYS": "suscripcion=k1, riesgo=k2 ,k3"}
    )

    assert config.api_keys == {"k1": "suscripcion", "k2": "riesgo", "k3": "servicio"}


def test_sin_claves_configuradas_el_mapa_queda_vacio():
    assert ConfigPerfilamiento.desde_entorno({}).api_keys == {}
