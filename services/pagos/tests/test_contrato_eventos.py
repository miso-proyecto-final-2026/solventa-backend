"""Contrato de eventos con MS Suscripción (integración con HU15): lo que la saga envía y lo que Pagos responde.

Los ejemplos de `tests/contratos/` se generaron con el código real de cada productor (ver su README).
"""

import json
from pathlib import Path

import pytest
from app.eventos import Evento
from app.pagos.modelos_db import EventoSalidaDB
from sqlalchemy import select

from tests.dobles import PasarelaFalsa

CONTRATOS = Path(__file__).parent / "contratos"


def _ejemplo(nombre: str) -> Evento:
    return Evento.model_validate_json((CONTRATOS / f"{nombre}.json").read_text(encoding="utf-8"))


async def _cobrar_el_comando_de_la_saga(armar, fabrica, guion):
    servicio = armar(PasarelaFalsa(guion))
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(_ejemplo("cobro_prima_solicitado"), s)
    await servicio.procesar_pendientes()
    with fabrica() as s:
        (salida,) = s.execute(select(EventoSalidaDB)).scalars()
        return salida.payload


@pytest.mark.parametrize(("guion", "esperado"), [("aprobar", "cobro_prima_confirmado"), ("rechazar", "cobro_prima_rechazado")])
async def test_el_comando_de_la_saga_produce_el_resultado_con_la_forma_publicada(armar, fabrica, guion, esperado):
    emitido = await _cobrar_el_comando_de_la_saga(armar, fabrica, guion)
    publicado = json.loads((CONTRATOS / f"{esperado}.json").read_text(encoding="utf-8"))

    assert emitido["event_type"] == publicado["event_type"] == esperado
    assert set(emitido) == set(publicado)
    assert set(emitido["payload"]) == set(publicado["payload"])  # el consumidor (Suscripción) lee estos campos
    comando = _ejemplo("cobro_prima_solicitado")
    assert emitido["payload"]["suscripcion_id"] == comando.payload["suscripcion_id"]
    assert (emitido["correlation_id"], emitido["trace_id"]) == (comando.correlation_id, comando.trace_id)


def test_las_copias_de_los_contratos_son_identicas_en_los_dos_servicios():
    otra = Path(__file__).parents[2] / "suscripcion" / "tests" / "contratos"
    if not otra.exists():
        pytest.skip("MS Suscripción no está en este árbol")
    for archivo in CONTRATOS.glob("*.json"):
        assert archivo.read_text(encoding="utf-8") == (otra / archivo.name).read_text(encoding="utf-8"), archivo.name
