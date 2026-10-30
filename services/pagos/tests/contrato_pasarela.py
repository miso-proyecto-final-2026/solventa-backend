"""Suite de contrato del puerto `PasarelaPago` (ADR-0004): todo adaptador debe pasarla antes de aceptarse.

Cada adaptador entrega una fábrica `preparar(escenario) -> PasarelaPago` con los escenarios de abajo. La suite no conoce
al proveedor: solo usa el puerto y los tipos del dominio.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from decimal import Decimal

import pytest
from app.pagos.dominio import (
    DatosToken,
    PasarelaPago,
    PasarelaSinRespuesta,
    ResultadoCobro,
    SolicitudCobro,
    TokenDesconocido,
)

ESCENARIOS = ("aprobar", "rechazar", "perder_respuesta")
TOKEN = "tok_contrato_01"
Preparar = Callable[[str], PasarelaPago]


def _solicitud(clave: str = "k-contrato", monto: str = "175000.00") -> SolicitudCobro:
    return SolicitudCobro(idempotency_key=clave, token=TOKEN, monto=Decimal(monto), moneda="COP", referencia="susc-1")


class ContratoPasarela:
    """Heredar y definir el fixture `preparar`."""

    def test_implementa_el_puerto(self, preparar: Preparar):
        pasarela = preparar("aprobar")
        for metodo in ("cobrar", "consultar", "reembolsar", "describir_token"):
            assert inspect.iscoroutinefunction(getattr(pasarela, metodo)), metodo

    async def test_aprobar_devuelve_un_resultado_del_dominio_con_referencia(self, preparar: Preparar):
        resultado = await preparar("aprobar").cobrar(_solicitud())
        assert isinstance(resultado, ResultadoCobro)
        assert resultado.aprobado and resultado.referencia_pasarela

    async def test_rechazar_trae_codigo_y_motivo(self, preparar: Preparar):
        resultado = await preparar("rechazar").cobrar(_solicitud())
        assert not resultado.aprobado and resultado.codigo and resultado.motivo

    async def test_la_misma_clave_no_cobra_dos_veces_y_devuelve_el_primer_resultado(self, preparar: Preparar):
        pasarela = preparar("aprobar")
        primero = await pasarela.cobrar(_solicitud())
        segundo = await pasarela.cobrar(_solicitud())
        assert primero == segundo

    async def test_consultar_una_clave_desconocida_devuelve_none(self, preparar: Preparar):
        assert await preparar("aprobar").consultar("nunca-cobrada") is None

    async def test_consultar_devuelve_el_resultado_del_cobro(self, preparar: Preparar):
        pasarela = preparar("rechazar")
        cobro = await pasarela.cobrar(_solicitud())
        assert await pasarela.consultar("k-contrato") == cobro

    async def test_respuesta_perdida_se_informa_como_sin_respuesta_y_consultar_si_la_ve(self, preparar: Preparar):
        pasarela = preparar("perder_respuesta")
        with pytest.raises(PasarelaSinRespuesta):
            await pasarela.cobrar(_solicitud())
        conciliado = await pasarela.consultar("k-contrato")
        assert conciliado is not None and conciliado.aprobado

    async def test_reembolsar_devuelve_un_resultado_aprobado_con_referencia(self, preparar: Preparar):
        pasarela = preparar("aprobar")
        await pasarela.cobrar(_solicitud())
        resultado = await pasarela.reembolsar(_solicitud("reembolso-contrato"))
        assert isinstance(resultado, ResultadoCobro)
        assert resultado.aprobado and resultado.referencia_pasarela

    async def test_la_misma_clave_no_reembolsa_dos_veces(self, preparar: Preparar):
        pasarela = preparar("aprobar")
        await pasarela.cobrar(_solicitud())
        primero = await pasarela.reembolsar(_solicitud("reembolso-contrato"))
        segundo = await pasarela.reembolsar(_solicitud("reembolso-contrato"))
        assert primero == segundo

    async def test_describir_token_da_marca_y_cuatro_digitos_sin_el_token(self, preparar: Preparar):
        datos = await preparar("aprobar").describir_token(TOKEN)
        assert isinstance(datos, DatosToken)
        assert len(datos.ultimos4) == 4 and datos.ultimos4.isdigit() and datos.marca
        assert TOKEN not in repr(datos)

    async def test_un_token_que_no_reconoce_falla_con_un_error_del_dominio(self, preparar: Preparar):
        with pytest.raises(TokenDesconocido):
            await preparar("aprobar").describir_token("4111111111111111")

    async def test_los_errores_no_revelan_el_token(self, preparar: Preparar):
        pasarela = preparar("perder_respuesta")
        with pytest.raises(PasarelaSinRespuesta) as error:
            await pasarela.cobrar(_solicitud())
        assert TOKEN not in str(error.value)
