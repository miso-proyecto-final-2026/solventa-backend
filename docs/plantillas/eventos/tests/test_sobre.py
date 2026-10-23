"""Sobre de evento estándar (documentación técnica §4.3)."""

import json
from datetime import UTC, datetime

import pytest
from eventos import Evento, EventoInvalido


def _evento(**cambios):
    datos = {"event_type": "cobro_prima_solicitado", "source": "ms-suscripcion", "correlation_id": "saga-1", "payload": {"monto": 45000}}
    return Evento(**{**datos, **cambios})


def test_los_campos_del_sobre_siguen_la_documentacion():
    cuerpo = json.loads(_evento().a_bytes())

    assert set(cuerpo) == {"event_id", "event_type", "source", "timestamp", "correlation_id", "trace_id", "version", "payload"}
    assert cuerpo["version"] == "1.0"
    assert cuerpo["payload"] == {"monto": 45000}


def test_cada_evento_nace_con_ids_distintos_y_hora_utc():
    a, b = _evento(), _evento()

    assert a.event_id != b.event_id and a.trace_id != b.trace_id
    assert a.timestamp.tzinfo is not None and a.timestamp.utcoffset() == UTC.utcoffset(None)
    assert abs((datetime.now(UTC) - a.timestamp).total_seconds()) < 5


def test_ida_y_vuelta_conserva_el_evento():
    original = _evento(payload={"anidado": {"lista": [1, 2, 3]}})

    assert Evento.desde_bytes(original.a_bytes()) == original


def test_el_ejemplo_de_la_documentacion_se_lee_tal_cual():
    ejemplo = (
        b'{"event_id":"550e8400-e29b-41d4-a716-446655440000","event_type":"cobro_prima_solicitado",'
        b'"source":"ms-suscripcion","timestamp":"2026-09-27T10:30:00Z","correlation_id":"saga-123-456",'
        b'"trace_id":"trace-789","version":"1.0","payload":{"suscripcion_id":"uuid","monto":45000,"moneda":"COP"}}'
    )

    evento = Evento.desde_bytes(ejemplo)

    assert evento.event_id == "550e8400-e29b-41d4-a716-446655440000" and evento.payload["monto"] == 45000


@pytest.mark.parametrize("basura", [b"", b"no es json", b"[]", b'{"event_type": "x"}'])
def test_un_sobre_ilegible_o_incompleto_es_evento_invalido(basura):
    with pytest.raises(EventoInvalido):
        Evento.desde_bytes(basura)


@pytest.mark.parametrize("tipo", ["", "Con Espacios", "mayusculas_Malas", "1empieza_con_digito"])
def test_el_tipo_de_evento_es_snake_case(tipo):
    with pytest.raises(ValueError):
        _evento(event_type=tipo)


def test_el_payload_tiene_que_ser_un_objeto():
    with pytest.raises(ValueError):
        _evento(payload=[1, 2])


def test_los_campos_desconocidos_se_ignoran_para_poder_evolucionar_el_contrato():
    datos = json.loads(_evento().a_bytes())
    datos["campo_nuevo"] = "de una version futura"

    assert Evento.desde_bytes(json.dumps(datos).encode()).event_type == "cobro_prima_solicitado"
