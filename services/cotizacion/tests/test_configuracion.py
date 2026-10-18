"""Fuera de desarrollo, una variable faltante impide arrancar (falla cerrado)."""

import pytest
from fastapi.testclient import TestClient

from app.configuracion import (
    VARIABLE_MODO_DESARROLLO,
    ErrorConfiguracion,
    modo_desarrollo,
)
from app.cotizacion.config import ConfigCotizacion
from app.cotizacion.wiring import construir_servicio_cotizacion
from app.main import app
from app.proveedores.cache import CacheMemoria, CacheRedis
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoHttp, ConsentimientoStub
from app.proveedores.wiring import construir_servicio

VARIABLES = (
    "REDIS_URL",
    "IDENTIDAD_URL",
    "COTIZACION_DATABASE_URL",
    "DATABASE_URL",
)


@pytest.fixture
def produccion(monkeypatch):
    """Entorno sin modo desarrollo y sin ninguna de las URL de infraestructura."""
    monkeypatch.delenv(VARIABLE_MODO_DESARROLLO, raising=False)
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


@pytest.mark.parametrize("valor", ["1", "true", "TRUE", " si ", "yes"])
def test_valores_que_activan_el_modo_desarrollo(valor):
    assert modo_desarrollo({VARIABLE_MODO_DESARROLLO: valor})


@pytest.mark.parametrize("valor", ["", "0", "false", "no", "produccion"])
def test_cualquier_otro_valor_no_activa_el_modo_desarrollo(valor):
    assert not modo_desarrollo({VARIABLE_MODO_DESARROLLO: valor})


def test_sin_redis_no_arranca_fuera_de_desarrollo(produccion):
    produccion.setenv("IDENTIDAD_URL", "http://identidad")

    with pytest.raises(ErrorConfiguracion, match="REDIS_URL"):
        construir_servicio()


def test_sin_identidad_no_arranca_fuera_de_desarrollo(produccion):
    produccion.setenv("REDIS_URL", "redis://localhost:6379/0")

    with pytest.raises(ErrorConfiguracion, match="IDENTIDAD_URL"):
        construir_servicio()


def test_el_mensaje_indica_como_trabajar_en_local(produccion):
    with pytest.raises(ErrorConfiguracion, match=VARIABLE_MODO_DESARROLLO):
        construir_servicio()


def test_con_toda_la_configuracion_usa_las_dependencias_reales(produccion):
    produccion.setenv("REDIS_URL", "redis://localhost:6379/0")
    produccion.setenv("IDENTIDAD_URL", "http://identidad")

    servicio = construir_servicio()

    assert isinstance(servicio._cache, CacheRedis)
    assert isinstance(servicio._consentimiento, ConsentimientoHttp)


def test_en_desarrollo_usa_los_sustitutos_locales(monkeypatch):
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    servicio = construir_servicio()

    assert isinstance(servicio._cache, CacheMemoria)
    assert isinstance(servicio._consentimiento, ConsentimientoStub)


def test_en_desarrollo_se_avisa_de_cada_sustituto(monkeypatch, caplog):
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)

    with caplog.at_level("WARNING"):
        construir_servicio()

    avisos = " ".join(r.getMessage() for r in caplog.records)
    assert "REDIS_URL" in avisos and "IDENTIDAD_URL" in avisos


def test_sin_base_de_datos_no_arranca_fuera_de_desarrollo(produccion):
    config = ConfigCotizacion.desde_entorno()
    servicio_perfil = object()  # no se llega a usarlo: falla antes de ensamblar

    with pytest.raises(ErrorConfiguracion, match="COTIZACION_DATABASE_URL"):
        construir_servicio_cotizacion(servicio_perfil, config)  # type: ignore[arg-type]


def test_con_base_de_datos_explicita_arranca_fuera_de_desarrollo(produccion):
    produccion.setenv("COTIZACION_DATABASE_URL", "sqlite:///:memory:")
    config = ConfigCotizacion.desde_entorno()

    servicio, engine = construir_servicio_cotizacion(object(), config)  # type: ignore[arg-type]

    assert servicio is not None
    engine.dispose()


def test_en_desarrollo_la_base_por_defecto_es_sqlite_local(monkeypatch, tmp_path):
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")
    monkeypatch.delenv("COTIZACION_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    _, engine = construir_servicio_cotizacion(
        object(),  # type: ignore[arg-type]
        ConfigCotizacion.desde_entorno(),
    )

    assert engine.dialect.name == "sqlite"
    assert (tmp_path / "solventa.db").exists()
    engine.dispose()


def test_la_app_no_arranca_sin_configuracion_fuera_de_desarrollo(produccion):
    with pytest.raises(ErrorConfiguracion), TestClient(app):
        pass


def test_la_configuracion_por_defecto_no_trae_url_de_base_de_datos():
    assert ConfigCotizacion().database_url is None
    assert ConfigProveedores().redis_url is None
