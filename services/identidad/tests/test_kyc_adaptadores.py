"""El mismo contrato del puerto `VerificadorIdentidad`, ejecutado contra cada adaptador (HA06)."""

import pytest

from app.kyc.simulador import (
    ConfigSimulador,
    ModoSimulador,
    ProveedorKycSimulado,
    VerificadorSimulado,
)
from tests.contrato_verificador import ContratoVerificador, Escenario


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
        }[escenario]
        self.proveedor.config = ConfigSimulador(modo=modo)

    def casos_abiertos(self) -> int:
        return self.proveedor.casos_abiertos


class TestContratoAdaptadorSimulado(ContratoVerificador):
    @pytest.fixture
    def banco(self):
        return BancoSimulado()
