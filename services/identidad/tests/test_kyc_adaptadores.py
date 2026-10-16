"""El mismo contrato del puerto `VerificadorIdentidad`, ejecutado contra cada adaptador (HA06)."""

import httpx
import pytest

from app.kyc.proveedor_b import ConfigProveedorB, VerificadorProveedorB
from app.kyc.simulador import (
    ConfigSimulador,
    ModoSimulador,
    ProveedorKycSimulado,
    VerificadorSimulado,
)
from tests.contrato_verificador import ContratoVerificador, Escenario
from tests.proveedor_b_falso import ModoB, ProveedorBFalso


class BancoSimulado:
    """Proveedor simulado en proceso (adaptador original de HU02)."""

    def __init__(self):
        self.proveedor = ProveedorKycSimulado()
        self.verificador = VerificadorSimulado(self.proveedor)

    def preparar(self, escenario: Escenario) -> None:
        if escenario is Escenario.VACIO:

            async def respuesta_vacia(clave, reto):
                return {}

            self.proveedor.crear_caso = respuesta_vacia
            return
        modo = {
            Escenario.EXITO: ModoSimulador.EXITO,
            Escenario.RECHAZO: ModoSimulador.RECHAZO,
            Escenario.TIMEOUT: ModoSimulador.TIMEOUT,
            Escenario.ERROR_5XX: ModoSimulador.ERROR_5XX,
            Escenario.CAIDO: ModoSimulador.ERROR_5XX,  # el simulador lo expresa como ConnectionError
        }[escenario]
        self.proveedor.config = ConfigSimulador(modo=modo)

    def casos_abiertos(self) -> int:
        return self.proveedor.casos_abiertos


class TestContratoAdaptadorSimulado(ContratoVerificador):
    @pytest.fixture
    def banco(self):
        return BancoSimulado()


class BancoProveedorB:
    """Proveedor B falso con su propio contrato HTTP (HA06)."""

    def __init__(self):
        self.proveedor = ProveedorBFalso()
        cliente = httpx.AsyncClient(base_url="https://kyc-b.test", transport=self.proveedor.transport)
        config = ConfigProveedorB("https://kyc-b.test", self.proveedor.api_key)
        self.verificador = VerificadorProveedorB(config, cliente)

    def preparar(self, escenario: Escenario) -> None:
        self.proveedor.modo = {
            Escenario.EXITO: ModoB.PASS,
            Escenario.RECHAZO: ModoB.FAIL,
            Escenario.TIMEOUT: ModoB.HANG,
            Escenario.ERROR_5XX: ModoB.ERROR_503,
            Escenario.CAIDO: ModoB.CAIDO,
            Escenario.VACIO: ModoB.VACIO,
        }[escenario]

    def casos_abiertos(self) -> int:
        return len(self.proveedor.casos)


class TestContratoProveedorB(ContratoVerificador):
    @pytest.fixture
    def banco(self):
        return BancoProveedorB()
