"""Suite de contrato del puerto `VerificadorIdentidad` (HA06). Cualquier proveedor debe pasarla.

No es un módulo de pruebas por sí mismo: cada adaptador la hereda en una clase `Test...` y aporta
su `banco`, el objeto que sabe poner a *su* proveedor (simulado o falso) en cada escenario.
La suite solo habla en términos del dominio, nunca del contrato de un proveedor concreto.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Protocol

import pytest

from app.kyc.dominio import (
    RETO_VIDA,
    DecisionProveedor,
    ImagenesKyc,
    ProveedorKycError,
    ResultadoVerificacion,
    SolicitudVerificacion,
    VerificadorIdentidad,
)

MARCA_IMAGEN = b"CONTENIDO-SENSIBLE-DE-LA-IMAGEN"
TIMEOUT_PRUEBA_S = 0.3


class Escenario(str, Enum):
    EXITO = "exito"
    RECHAZO = "rechazo"
    TIMEOUT = "timeout"
    ERROR_5XX = "error_5xx"
    VACIO = "vacio"


class Banco(Protocol):
    """Lo que cada adaptador aporta a la suite."""

    verificador: VerificadorIdentidad

    def preparar(self, escenario: Escenario) -> None:
        """Pone al proveedor en el comportamiento pedido."""

    def casos_abiertos(self) -> int:
        """Casos distintos que el proveedor llegó a abrir."""


def solicitud(clave: str = "clave-0001", reto: str = RETO_VIDA) -> SolicitudVerificacion:
    imagenes = ImagenesKyc(MARCA_IMAGEN + b"-a", MARCA_IMAGEN + b"-r", MARCA_IMAGEN + b"-s")
    return SolicitudVerificacion("c1", clave, reto, imagenes)


class ContratoVerificador:
    """Pruebas comunes. Las subclases definen la fixture `banco`."""

    async def test_exito_se_traduce_a_decision_aprobada(self, banco: Banco):
        banco.preparar(Escenario.EXITO)

        resultado = await banco.verificador.verificar(solicitud())

        assert isinstance(resultado, ResultadoVerificacion)
        assert resultado.decision is DecisionProveedor.APROBADA
        assert resultado.referencia

    async def test_rechazo_se_traduce_a_decision_rechazada(self, banco: Banco):
        banco.preparar(Escenario.RECHAZO)

        resultado = await banco.verificador.verificar(solicitud())

        assert resultado.decision is DecisionProveedor.RECHAZADA
        assert resultado.referencia

    async def test_un_reto_distinto_al_definido_no_supera_la_prueba_de_vida(self, banco: Banco):
        banco.preparar(Escenario.EXITO)

        resultado = await banco.verificador.verificar(solicitud(reto="SONREIR"))

        assert resultado.decision is DecisionProveedor.RECHAZADA

    async def test_un_proveedor_lento_no_responde_ni_falla_de_otra_forma(self, banco: Banco):
        banco.preparar(Escenario.TIMEOUT)

        # El núcleo corta con su timeout duro; el adaptador no puede devolver un resultado
        # inventado ni lanzar una excepción ajena al dominio.
        with pytest.raises((TimeoutError, ProveedorKycError)):
            await asyncio.wait_for(banco.verificador.verificar(solicitud()), TIMEOUT_PRUEBA_S)

    async def test_un_error_5xx_se_traduce_a_error_del_dominio(self, banco: Banco):
        banco.preparar(Escenario.ERROR_5XX)

        with pytest.raises(ProveedorKycError):
            await banco.verificador.verificar(solicitud())

    async def test_una_respuesta_vacia_se_traduce_a_error_del_dominio(self, banco: Banco):
        banco.preparar(Escenario.VACIO)

        with pytest.raises(ProveedorKycError):
            await banco.verificador.verificar(solicitud())

    async def test_la_misma_clave_no_abre_dos_casos_en_el_proveedor(self, banco: Banco):
        banco.preparar(Escenario.EXITO)

        primero = await banco.verificador.verificar(solicitud(clave="misma-clave"))
        segundo = await banco.verificador.verificar(solicitud(clave="misma-clave"))
        await banco.verificador.verificar(solicitud(clave="otra-clave"))

        assert primero.referencia == segundo.referencia
        assert banco.casos_abiertos() == 2

    @pytest.mark.parametrize("escenario", [Escenario.ERROR_5XX, Escenario.VACIO])
    async def test_los_errores_no_filtran_el_contenido_de_las_imagenes(
        self, banco: Banco, escenario: Escenario
    ):
        banco.preparar(escenario)

        with pytest.raises(ProveedorKycError) as error:
            await banco.verificador.verificar(solicitud())

        assert MARCA_IMAGEN.decode() not in str(error.value)
        assert MARCA_IMAGEN.decode() not in repr(error.value)
