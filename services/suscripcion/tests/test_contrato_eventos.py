"""Contrato de eventos con MS Pagos (HU18): la saga pide el cobro y llega hasta COBRO_REALIZADO o hasta el rechazo.

Los ejemplos de `tests/contratos/` se generaron con el código real de cada productor (ver su README).
"""

import json
from pathlib import Path

import pytest
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.cobros import manejar_resultado_cobro

from tests.test_servicio import _iniciar, _salidas

CONTRATOS = Path(__file__).parent / "contratos"


def _ejemplo(nombre: str, suscripcion_id: str) -> Evento:
    """El ejemplo publicado, dirigido a la saga de la prueba."""
    evento = Evento.model_validate_json((CONTRATOS / f"{nombre}.json").read_text(encoding="utf-8"))
    return evento.model_copy(update={"payload": {**evento.payload, "suscripcion_id": suscripcion_id}})


async def _saga_en_el_cobro(armar):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)
    return susc


async def test_el_comando_de_cobro_tiene_la_forma_publicada(armar, fabrica):
    await _saga_en_el_cobro(armar)
    (salida,) = _salidas(fabrica)
    publicado = json.loads((CONTRATOS / "cobro_prima_solicitado.json").read_text(encoding="utf-8"))

    assert salida.payload["event_type"] == publicado["event_type"]
    assert set(salida.payload) == set(publicado)
    assert set(salida.payload["payload"]) == set(publicado["payload"])  # el consumidor (Pagos) lee estos campos


@pytest.mark.parametrize(
    ("nombre", "estado_saga", "paso", "cobro"),
    [
        ("cobro_prima_confirmado", "COBRO_REALIZADO", "FIRMA", "APROBADO"),
        ("cobro_prima_rechazado", "APROBADA", "COBRO", "RECHAZADO"),
    ],
)
async def test_la_saga_aplica_el_resultado_que_publica_pagos(armar, fabrica, nombre, estado_saga, paso, cobro):
    susc = await _saga_en_el_cobro(armar)
    evento = _ejemplo(nombre, susc.id)

    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(evento, s)

    with fabrica() as s:
        actual = repo.obtener(s, susc.id)
    assert (actual.estado_saga, actual.paso_actual, actual.cobro_estado) == (estado_saga, paso, cobro)
    assert actual.cobro_transaccion_id == evento.payload["transaccion_id"]
    if cobro == "RECHAZADO":
        assert actual.cobro_codigo == evento.payload["codigo"] and actual.cobro_gracia_hasta is not None


# --- compensación de la firma (HU21) -------------------------------------------------------------------------------

async def test_el_comando_de_reembolso_tiene_la_forma_publicada(armar, fabrica):
    from datetime import timedelta

    from tests.test_firma import COBRO, _comandos, _lista

    firma, _, _, reloj = await _lista(armar, fabrica)
    reloj.ahora = COBRO + timedelta(hours=25)
    await firma.compensar_vencidas()
    (salida,) = [c for c in _comandos(fabrica) if c.payload["event_type"] == "reembolso_cobro_solicitado"]
    publicado = json.loads((CONTRATOS / "reembolso_cobro_solicitado.json").read_text(encoding="utf-8"))

    assert set(salida.payload) == set(publicado)
    assert set(salida.payload["payload"]) == set(publicado["payload"])  # el consumidor (Pagos) lee estos campos


async def test_la_saga_aplica_el_reembolso_que_publica_pagos(armar, fabrica):
    from datetime import timedelta

    from tests.test_firma import COBRO, _lista

    firma, susc, _, reloj = await _lista(armar, fabrica)
    reloj.ahora = COBRO + timedelta(hours=25)
    await firma.compensar_vencidas()
    evento = _ejemplo("cobro_prima_reembolsado", susc.id)

    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(evento, s)

    with fabrica() as s:
        actual = repo.obtener(s, susc.id)
    assert (actual.estado_saga, actual.compensacion_estado) == ("COMPENSADA", "COMPLETADA")
