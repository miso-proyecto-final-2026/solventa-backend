"""Cada evento publicado y consumido deja un log JSON con sus metadatos y sin el contenido del evento."""

import logging
from types import SimpleNamespace

from app.eventos import Evento
from app.eventos.consumidor import ConsumidorEventos
from app.eventos.publicador import PublicadorKafka


class _ProductorFalso:
    async def send_and_wait(self, topic, valor, key, headers):
        return SimpleNamespace(partition=2, offset=7)


class _Sesion:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def begin(self):
        return self


class _Registro:
    def __init__(self, nuevo):
        self.nuevo = nuevo

    def reservar(self, sesion, grupo, event_id):
        return self.nuevo


def _evento():
    return Evento(event_type="evento_de_prueba", source="ms-prueba", correlation_id="c-1", payload={"secreto": "no-debe-salir"})


def _campos(caplog, nombre):
    (registro,) = [r for r in caplog.records if r.getMessage() == nombre or getattr(r, "campos", {}).get("evento") == nombre]
    return registro.campos


async def test_publicar_registra_topic_particion_offset_y_event_id(caplog):
    publicador = PublicadorKafka(["localhost:9092"], {}, client_id="prueba")
    publicador._productor = _ProductorFalso()
    evento = _evento()
    with caplog.at_level(logging.INFO, logger="solventa.eventos"):
        await publicador.publicar("solventa.prueba", evento, clave="k-1")
    campos = _campos(caplog, "evento_publicado")
    assert campos["topic"] == "solventa.prueba"
    assert (campos["particion"], campos["offset"]) == (2, 7)
    assert campos["clave"] == "k-1"
    assert campos["event_id"] == evento.event_id
    assert campos["event_type"] == "evento_de_prueba"
    assert "no-debe-salir" not in caplog.text


async def _consumir(nuevo):
    async def manejador(evento, sesion):
        return None

    consumidor = ConsumidorEventos(
        brokers=["localhost:9092"],
        seguridad={},
        topic="solventa.prueba",
        grupo="svc-prueba",
        manejador=manejador,
        fabrica_sesiones=_Sesion,
        registro=_Registro(nuevo),
        publicador=None,
    )
    evento = _evento()
    await consumidor.procesar(SimpleNamespace(value=evento.a_bytes(), key=b"k-1", partition=1, offset=42))
    return evento


async def test_consumir_registra_el_evento_consumido(caplog):
    with caplog.at_level(logging.INFO, logger="solventa.eventos"):
        evento = await _consumir(nuevo=True)
    campos = _campos(caplog, "evento_consumido")
    assert (campos["topic"], campos["grupo"]) == ("solventa.prueba", "svc-prueba")
    assert (campos["particion"], campos["offset"]) == (1, 42)
    assert campos["event_id"] == evento.event_id
    assert "no-debe-salir" not in caplog.text


async def test_una_reentrega_se_registra_como_omitida(caplog):
    with caplog.at_level(logging.INFO, logger="solventa.eventos"):
        await _consumir(nuevo=False)
    assert _campos(caplog, "evento_reentrega_omitida")["offset"] == 42
