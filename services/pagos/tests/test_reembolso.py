"""Reembolso de la compensación de la saga (HU21): MS Suscripción lo pide cuando la firma falla o vence."""

from decimal import Decimal

import pytest
from app.pagos.errores import SolicitudInvalida
from app.pagos.modelos_db import TransaccionDB
from app.pagos.simulador import ModoSimulador, PasarelaSimulada
from sqlalchemy import select

from tests.dobles import PasarelaFalsa, evento_reembolso
from tests.test_servicio import _recibir, _salidas, _transacciones


async def _cobrada(armar, fabrica, pasarela=None):
    pasarela = pasarela or PasarelaFalsa()
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()
    return servicio, pasarela


def _reembolsos(fabrica):
    with fabrica() as s:
        return list(s.execute(select(TransaccionDB).where(TransaccionDB.tipo == "REEMBOLSO_PRIMA")).scalars())


async def test_un_reembolso_devuelve_el_cobro_aprobado_y_publica_el_resultado(armar, fabrica):
    servicio, pasarela = await _cobrada(armar, fabrica)

    await _recibir(servicio, fabrica, evento_reembolso())
    assert await servicio.procesar_pendientes() == 1

    (reembolso,) = _reembolsos(fabrica)
    assert (reembolso.estado, reembolso.referencia_pasarela) == ("APROBADA", "rf-1")
    assert (Decimal(reembolso.monto), reembolso.moneda) == (Decimal("175000.00"), "COP")
    assert pasarela.ordenes_reembolso[0].idempotency_key == "reembolso_prima:susc-1"
    ultimo = _salidas(fabrica)[-1]
    assert ultimo["event_type"] == "cobro_prima_reembolsado" and ultimo["source"] == "ms-pagos"
    assert ultimo["payload"]["suscripcion_id"] == "susc-1" and ultimo["payload"]["tipo"] == "REEMBOLSO_PRIMA"


async def test_repetir_el_comando_no_reembolsa_dos_veces_y_repite_el_resultado(armar, fabrica):
    servicio, pasarela = await _cobrada(armar, fabrica)
    await _recibir(servicio, fabrica, evento_reembolso())
    await servicio.procesar_pendientes()

    await _recibir(servicio, fabrica, evento_reembolso())
    await servicio.procesar_pendientes()

    assert len(_reembolsos(fabrica)) == 1
    assert len(pasarela.reembolsos) == 1
    anunciados = [x for x in _salidas(fabrica) if x["event_type"] == "cobro_prima_reembolsado"]
    assert [x["payload"]["repeticion"] for x in anunciados] == [False, True]


async def test_sin_cobro_aprobado_el_comando_se_rechaza_para_que_vaya_a_la_cola_de_fallidos(armar, fabrica):
    servicio = armar(PasarelaFalsa())

    with pytest.raises(SolicitudInvalida):
        await _recibir(servicio, fabrica, evento_reembolso())

    assert _reembolsos(fabrica) == []


async def test_un_cobro_rechazado_no_se_reembolsa(armar, fabrica):
    servicio, _ = await _cobrada(armar, fabrica, PasarelaFalsa("rechazar"))

    with pytest.raises(SolicitudInvalida):
        await _recibir(servicio, fabrica, evento_reembolso())


async def test_si_la_pasarela_no_responde_el_reembolso_se_reintenta_sin_perderse(armar, fabrica, reloj):
    servicio, pasarela = await _cobrada(armar, fabrica)
    await _recibir(servicio, fabrica, evento_reembolso())
    pasarela.reembolso_caido = True
    await servicio.procesar_pendientes()
    (reembolso,) = _reembolsos(fabrica)
    assert reembolso.estado == "PENDIENTE"

    pasarela.reembolso_caido = False
    reloj.avanzar(600)
    await servicio.procesar_pendientes()

    (reembolso,) = _reembolsos(fabrica)
    assert reembolso.estado == "APROBADA"
    assert len(pasarela.reembolsos) == 1


async def test_un_reembolso_no_abre_un_caso_de_cobro_fallido(armar, fabrica):
    servicio, _pasarela = await _cobrada(armar, fabrica)
    await _recibir(servicio, fabrica, evento_reembolso())
    await servicio.procesar_pendientes()

    with fabrica() as s:
        assert s.execute(select(TransaccionDB).where(TransaccionDB.estado == "RECHAZADA")).first() is None
    assert [t.tipo for t in _transacciones(fabrica)].count("COBRO_PRIMA") == 1


async def test_el_simulador_cuenta_el_reembolso_aparte_del_cobro(fabrica):
    from app.pagos.dominio import SolicitudCobro

    pasarela = PasarelaSimulada(fabrica, modo=ModoSimulador.APROBAR)
    orden = SolicitudCobro("k-cobro", "tok_sim_x", Decimal(100), "COP", "susc-1")
    await pasarela.cobrar(orden)
    devolucion = SolicitudCobro("k-reembolso", "tok_sim_x", Decimal(100), "COP", "susc-1")
    primero = await pasarela.reembolsar(devolucion)
    segundo = await pasarela.reembolsar(devolucion)

    assert primero == segundo and primero.referencia_pasarela.startswith("simrf_")
    assert (pasarela.cargos(), pasarela.reembolsos()) == (1, 1)


async def test_el_simulador_caido_no_recibe_el_reembolso(fabrica):
    from app.pagos.dominio import PasarelaNoDisponible, SolicitudCobro

    pasarela = PasarelaSimulada(fabrica)
    with pytest.raises(PasarelaNoDisponible):
        await pasarela.reembolsar(SolicitudCobro("k", "tok_sim_caida_1", Decimal(100), "COP", "susc-1"))
    assert pasarela.reembolsos() == 0
