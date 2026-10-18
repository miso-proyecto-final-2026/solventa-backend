import pytest

from app.configuracion import VARIABLE_MODO_DESARROLLO


@pytest.fixture(autouse=True)
def modo_desarrollo_por_defecto(monkeypatch):
    """La suite arranca la app sin Identidad ni Redis reales; las pruebas de
    `test_configuracion.py` quitan el modo para verificar que sin él no arranca."""
    monkeypatch.setenv(VARIABLE_MODO_DESARROLLO, "1")
