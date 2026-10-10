import random

import fakeredis
import pytest

from app.proveedores.cache import CacheRedis
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento, Fuente
from app.proveedores.servicio import ServicioPerfil
from app.proveedores.simuladores import (
    ConfigSimulador,
    ModoFalla,
    SimuladorOpenData,
    SimuladorOpenFinance,
)

RAPIDO = {"latencia_min_ms": 0.0, "latencia_max_ms": 1.0}


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def construir(redis):
    """Fábrica de servicio con simuladores configurables y registro de llamadas."""

    def _construir(
        modo_of=ModoFalla.NINGUNO, modo_od=ModoFalla.NINGUNO, consentimiento=None, **cfg
    ):
        llamadas = []
        config = ConfigProveedores(**{"timeout_s": 0.05, **cfg})
        proveedores = {
            Fuente.OPEN_FINANCE: SimuladorOpenFinance(
                ConfigSimulador(modo=modo_of, **RAPIDO)
            ),
            Fuente.OPEN_DATA: SimuladorOpenData(
                ConfigSimulador(modo=modo_od, **RAPIDO)
            ),
        }
        servicio = ServicioPerfil(
            proveedores,
            CacheRedis(redis),
            consentimiento or ConsentimientoStub(EstadoConsentimiento.OTORGADO),
            config,
            registrar=llamadas.append,
            rng=random.Random(3),
        )
        return servicio, proveedores, llamadas

    return _construir
