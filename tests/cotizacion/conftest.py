import pytest

from app.cotizacion.reglas import cargar_reglas


@pytest.fixture(scope="session")
def reglas():
    return cargar_reglas("v1")
