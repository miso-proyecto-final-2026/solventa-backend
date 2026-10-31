"""Contrato de `poliza_emitida` (HU20) visto desde el consumidor: el ejemplo que publica MS Suscripción se procesa tal cual.

El ejemplo de `tests/contratos/` es una copia del de MS Suscripción (los servicios no comparten código, ADR-0001).
"""

import json
from pathlib import Path

from app.eventos import Evento
from app.notificaciones.modelos_db import EnvioDB
from app.notificaciones.servicio import CAMPOS_REQUERIDOS
from sqlalchemy import select

from tests.dobles import CorreoFalso
from tests.test_servicio import _recibir

EJEMPLO = Path(__file__).parent / "contratos" / "poliza_emitida.json"


def _evento() -> Evento:
    return Evento.model_validate_json(EJEMPLO.read_text(encoding="utf-8"))


def test_el_ejemplo_trae_todos_los_campos_que_el_consumidor_necesita():
    assert set(CAMPOS_REQUERIDOS) <= set(_evento().payload)
    assert _evento().event_type == "poliza_emitida"


async def test_el_ejemplo_publicado_produce_los_dos_envios_y_un_correo_con_sus_datos(armar, fabrica):
    correo = CorreoFalso()
    servicio = armar(correo=correo)
    publicado = json.loads(EJEMPLO.read_text(encoding="utf-8"))["payload"]

    await _recibir(servicio, fabrica, _evento())
    await servicio.procesar_pendientes()

    with fabrica() as s:
        envios = list(s.execute(select(EnvioDB)).scalars())
    assert sorted(e.canal for e in envios) == ["CORREO", "PUSH"] and {e.estado for e in envios} == {"ENVIADO"}
    assert {e.evento_origen_id for e in envios} == {_evento().event_id}
    assert publicado["numero_poliza"] in correo.enviados[0].texto
