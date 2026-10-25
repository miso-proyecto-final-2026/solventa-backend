"""Paso COBRO de la saga (HU18): periodicidad elegida, comando a MS Pagos y resultado del cobro."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.errores import SolicitudInvalida

from tests.test_servicio import _iniciar, _salidas

RESUELTA = "2026-10-25T12:00:05+00:00"


async def _aprobada(armar, **cambios):
    servicio = armar()
    susc, _ = await _iniciar(servicio, **cambios)
    await servicio.decidir(susc.id)
    return servicio, susc


def _resultado(suscripcion_id, tipo="cobro_prima_confirmado", **extra):
    payload = {
        "transaccion_id": "tx-1",
        "suscripcion_id": suscripcion_id,
        "estado": "APROBADA" if tipo == "cobro_prima_confirmado" else "RECHAZADA",
        "referencia_pasarela": "sim_ref",
        "resuelta_en": RESUELTA,
        **extra,
    }
    return Evento(event_type=tipo, source="ms-pagos", correlation_id=suscripcion_id, payload=payload)


async def _aplicar(fabrica, evento):
    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(evento, s)
    with fabrica() as s:
        return repo.obtener(s, evento.payload["suscripcion_id"])


# --- periodicidad y comando -------------------------------------------------------------------------------------

async def test_por_defecto_se_cobra_la_prima_anual_con_la_clave_de_la_saga(armar, fabrica):
    _, susc = await _aprobada(armar)

    (salida,) = _salidas(fabrica)
    datos = salida.payload["payload"]
    assert (datos["periodicidad_pago"], datos["monto"]) == ("ANUAL", "175000.00")
    assert datos["idempotency_key"] == f"cobro_prima:{susc.id}"
    assert (datos["prima_mensual"], datos["prima_anual"]) == (14583, 175000)  # se conservan por compatibilidad


async def test_si_el_cliente_elige_mensual_se_cobra_el_primer_mes(armar, fabrica):
    _, susc = await _aprobada(armar, periodicidad_pago="MENSUAL")

    assert susc.periodicidad_pago == "MENSUAL"
    (salida,) = _salidas(fabrica)
    assert salida.payload["payload"]["monto"] == "14583.00"


async def test_una_periodicidad_desconocida_se_rechaza(armar):
    with pytest.raises(SolicitudInvalida) as error:
        await _iniciar(armar(), periodicidad_pago="SEMANAL")
    assert error.value.detalles[0]["campo"] == "periodicidad_pago"


# --- resultado del cobro ------------------------------------------------------------------------------------------

async def test_cobro_confirmado_lleva_la_saga_a_cobro_realizado_y_a_la_firma(armar, fabrica):
    _, susc = await _aprobada(armar)

    actual = await _aplicar(fabrica, _resultado(susc.id))

    assert (actual.estado_saga, actual.paso_actual) == ("COBRO_REALIZADO", "FIRMA")
    assert (actual.cobro_estado, actual.cobro_transaccion_id, actual.cobro_referencia) == ("APROBADO", "tx-1", "sim_ref")
    assert repo._utc(actual.cobro_resuelto_en) == datetime(2026, 10, 25, 12, 0, 5, tzinfo=UTC)


async def test_cobro_rechazado_deja_la_saga_en_el_cobro_con_motivo_y_gracia(armar, fabrica):
    _, susc = await _aprobada(armar)

    actual = await _aplicar(
        fabrica,
        _resultado(
            susc.id, "cobro_prima_rechazado",
            codigo="FONDOS_INSUFICIENTES", motivo="Sin fondos", gracia_hasta="2026-10-28T12:00:05+00:00",
        ),
    )

    assert (actual.estado_saga, actual.paso_actual) == ("APROBADA", "COBRO")
    assert (actual.cobro_estado, actual.cobro_codigo, actual.cobro_motivo) == ("RECHAZADO", "FONDOS_INSUFICIENTES", "Sin fondos")
    assert repo._utc(actual.cobro_gracia_hasta) == datetime(2026, 10, 28, 12, 0, 5, tzinfo=UTC)


async def test_un_resultado_repetido_o_tardio_no_cambia_una_saga_que_ya_avanzo(armar, fabrica):
    _, susc = await _aprobada(armar)
    await _aplicar(fabrica, _resultado(susc.id))

    repetido = await _aplicar(fabrica, _resultado(susc.id))
    tardio = await _aplicar(fabrica, _resultado(susc.id, "cobro_prima_rechazado", codigo="X"))

    assert (repetido.estado_saga, tardio.estado_saga, tardio.cobro_estado) == ("COBRO_REALIZADO", "COBRO_REALIZADO", "APROBADO")


async def test_un_resultado_para_una_saga_que_no_espera_cobro_se_ignora(armar, fabrica):
    servicio = armar()
    susc, _ = await _iniciar(servicio)  # sin decidir: no está en el paso COBRO

    actual = await _aplicar(fabrica, _resultado(susc.id))

    assert (actual.estado_saga, actual.cobro_estado) == ("EN_PROGRESO", None)


async def test_un_resultado_de_una_suscripcion_inexistente_no_falla(fabrica):
    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(_resultado("no-existe"), s)


async def test_otros_eventos_del_topic_se_ignoran(armar, fabrica):
    _, susc = await _aprobada(armar)
    actual = await _aplicar(fabrica, _resultado(susc.id, "indemnizacion_pagada"))
    assert actual.estado_saga == "APROBADA"


def test_el_monto_a_cobrar_respeta_los_decimales():
    from types import SimpleNamespace

    from app.suscripcion.servicio import monto_a_cobrar

    susc = SimpleNamespace(periodicidad_pago="MENSUAL", prima_mensual=Decimal("14583.33"), prima_anual=Decimal(175000))
    assert monto_a_cobrar(susc) == Decimal("14583.33")
