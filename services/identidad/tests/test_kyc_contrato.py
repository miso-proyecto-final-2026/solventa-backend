"""Contrato del puerto `VerificadorIdentidad` (base para HA06) y mapeo del adaptador simulado."""

import pytest

from app.kyc.dominio import (
    RETO_VIDA,
    DecisionProveedor,
    ImagenesKyc,
    ProveedorKycError,
    ResultadoVerificacion,
    SolicitudVerificacion,
)
from app.kyc.simulador import (
    ConfigSimulador,
    ModoSimulador,
    ProveedorKycSimulado,
    VerificadorSimulado,
)


def _solicitud(clave="clave-0001", reto=RETO_VIDA):
    return SolicitudVerificacion("c1", clave, reto, ImagenesKyc(b"a", b"b", b"c"))


def _verificador(modo):
    return VerificadorSimulado(ProveedorKycSimulado(ConfigSimulador(modo=modo)))


async def comprobar_contrato_del_puerto(verificador):
    """Lo que debe cumplir cualquier adaptador de un proveedor de KYC (reutilizable)."""
    resultado = await verificador.verificar(_solicitud())
    assert isinstance(resultado, ResultadoVerificacion)
    assert isinstance(resultado.decision, DecisionProveedor)
    assert resultado.referencia


async def test_el_adaptador_simulado_cumple_el_contrato_del_puerto():
    await comprobar_contrato_del_puerto(_verificador(ModoSimulador.EXITO))
    await comprobar_contrato_del_puerto(_verificador(ModoSimulador.RECHAZO))


async def test_exito_se_traduce_a_decision_aprobada():
    resultado = await _verificador(ModoSimulador.EXITO).verificar(_solicitud())

    assert resultado.decision is DecisionProveedor.APROBADA


async def test_rechazo_se_traduce_a_decision_rechazada():
    resultado = await _verificador(ModoSimulador.RECHAZO).verificar(_solicitud())

    assert resultado.decision is DecisionProveedor.RECHAZADA


async def test_un_reto_distinto_al_definido_no_supera_la_prueba_de_vida():
    resultado = await _verificador(ModoSimulador.EXITO).verificar(_solicitud(reto="SONREIR"))

    assert resultado.decision is DecisionProveedor.RECHAZADA


async def test_un_fallo_5xx_del_proveedor_se_traduce_a_error_del_dominio():
    with pytest.raises(ProveedorKycError):
        await _verificador(ModoSimulador.ERROR_5XX).verificar(_solicitud())


@pytest.mark.parametrize("crudo", [{}, {"match": True}, {"case_id": "x", "match": True}, None])
def test_una_respuesta_mal_formada_se_traduce_a_error_del_dominio(crudo):
    with pytest.raises(ProveedorKycError):
        VerificadorSimulado._mapear(crudo)


async def test_la_misma_clave_no_abre_dos_casos_en_el_proveedor():
    proveedor = ProveedorKycSimulado()
    verificador = VerificadorSimulado(proveedor)

    primero = await verificador.verificar(_solicitud(clave="misma-clave"))
    segundo = await verificador.verificar(_solicitud(clave="misma-clave"))
    await verificador.verificar(_solicitud(clave="otra-clave"))

    assert primero.referencia == segundo.referencia
    assert proveedor.casos_abiertos == 2


def test_el_simulador_se_configura_por_entorno():
    config = ConfigSimulador.desde_entorno({"KYC_SIMULADOR_MODO": "rechazo", "KYC_SIMULADOR_LATENCIA_S": "0.5"})

    assert config.modo is ModoSimulador.RECHAZO
    assert config.latencia_s == 0.5


def test_las_imagenes_no_aparecen_en_el_repr():
    imagenes = ImagenesKyc(b"BYTES-ANVERSO", b"BYTES-REVERSO", b"BYTES-SELFIE")

    assert "BYTES" not in repr(imagenes)
    assert "BYTES" not in repr(_solicitud())
