"""Contrato del evento `poliza_emitida` (HU20, criterio 6): lo que Siniestros y la analítica leerán del topic de ciclo de vida.

El ejemplo de `tests/contratos/poliza_emitida.json` se generó con el código real de `emitir_poliza` (ver el README de esa carpeta).
"""

import json
from pathlib import Path

from app.eventos import Evento
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.polizas import emitir_poliza
from jsonschema import validate

from tests.test_polizas import AlmacenMemoria, _cobrada, _generador, _poliza
from tests.test_servicio import _salidas

CONTRATOS = Path(__file__).parent / "contratos"
ESQUEMA_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "required": ["poliza_id", "suscripcion_id", "cliente_id", "numero_poliza", "producto", "vigencia_desde", "fecha_vencimiento", "fecha_emision"],
    "properties": {
        "poliza_id": {"type": "string"},
        "suscripcion_id": {"type": "string"},
        "cliente_id": {"type": "string"},
        "numero_poliza": {"type": "string", "pattern": "^[0-9A-HJKMNP-TV-Z]{20}$"},
        "producto": {"type": "string"},
        "vigencia_desde": {"type": "string", "format": "date-time"},
        "fecha_vencimiento": {"type": "string", "format": "date-time"},
        "fecha_emision": {"type": "string", "format": "date-time"},
    },
}


async def _publicado(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    await _generador(fabrica, AlmacenMemoria()).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)
    with fabrica() as s, s.begin():
        emitir_poliza(s, poliza.id, poliza.created_at)
    (salida,) = [x for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]
    return salida, poliza


async def test_el_evento_publicado_tiene_la_forma_del_ejemplo(armar, fabrica):
    salida, poliza = await _publicado(armar, fabrica)
    ejemplo = json.loads((CONTRATOS / "poliza_emitida.json").read_text(encoding="utf-8"))

    assert set(salida.payload) == set(ejemplo)
    assert salida.payload["event_type"] == ejemplo["event_type"] == "poliza_emitida"
    assert salida.payload["source"] == ejemplo["source"] == "ms-suscripcion"
    assert set(salida.payload["payload"]) == set(ejemplo["payload"])
    assert salida.clave == poliza.id  # el topic se particiona por poliza_id
    validate(salida.payload["payload"], ESQUEMA_PAYLOAD)


def test_el_ejemplo_publicado_cumple_el_esquema_y_es_un_sobre_valido():
    ejemplo = json.loads((CONTRATOS / "poliza_emitida.json").read_text(encoding="utf-8"))

    Evento.model_validate(ejemplo)
    validate(ejemplo["payload"], ESQUEMA_PAYLOAD)
    assert ejemplo["correlation_id"] and ejemplo["trace_id"]


async def test_el_evento_no_lleva_datos_personales(armar, fabrica):
    salida, _ = await _publicado(armar, fabrica)

    texto = json.dumps(salida.payload, ensure_ascii=False)

    for dato in ("Ana", "Pérez", "******300", "tok_"):
        assert dato not in texto
