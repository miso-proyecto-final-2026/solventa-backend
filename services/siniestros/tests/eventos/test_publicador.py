"""Publicación de eventos con el sobre estándar, clave de partición y confirmación de todos los réplicas."""

import json

import pytest
from aiokafka import AIOKafkaConsumer

from app.eventos import Evento
from app.eventos.conexion import parametros_seguridad
from app.eventos.publicador import PublicadorKafka, PublicadorMemoria

pytestmark = pytest.mark.contenedores


def _evento(**cambios):
    datos = {"event_type": "cobro_prima_solicitado", "source": "ms-suscripcion", "correlation_id": "saga-1", "payload": {"monto": 1}}
    return Evento(**{**datos, **cambios})


async def _leer(brokers, topic, cuantos=1):
    consumidor = AIOKafkaConsumer(
        topic, bootstrap_servers=brokers, group_id=None, auto_offset_reset="earliest", **parametros_seguridad("PLAINTEXT")
    )
    await consumidor.start()
    try:
        mensajes = []
        while len(mensajes) < cuantos:
            lote = await consumidor.getmany(timeout_ms=5000)
            for registros in lote.values():
                mensajes.extend(registros)
        return mensajes
    finally:
        await consumidor.stop()


async def test_publica_el_sobre_con_la_clave_y_las_cabeceras(kafka_brokers, topic):
    evento = _evento()
    async with PublicadorKafka(kafka_brokers, parametros_seguridad("PLAINTEXT"), client_id="prueba") as publicador:
        await publicador.publicar(topic, evento, clave="suscripcion-123")

    (mensaje,) = await _leer(kafka_brokers, topic)

    assert mensaje.key == b"suscripcion-123"
    assert Evento.desde_bytes(mensaje.value) == evento
    cabeceras = {k: v.decode() for k, v in mensaje.headers}
    assert cabeceras["event_type"] == "cobro_prima_solicitado" and cabeceras["correlation_id"] == "saga-1"


async def test_la_misma_clave_cae_en_la_misma_particion(kafka_brokers, topic):
    async with PublicadorKafka(kafka_brokers, parametros_seguridad("PLAINTEXT"), client_id="prueba") as publicador:
        for _ in range(5):
            await publicador.publicar(topic, _evento(), clave="misma-clave")

    mensajes = await _leer(kafka_brokers, topic, cuantos=5)

    assert len({m.partition for m in mensajes}) == 1


async def test_sin_clave_no_se_publica(kafka_brokers):
    async with PublicadorKafka(kafka_brokers, parametros_seguridad("PLAINTEXT"), client_id="prueba") as publicador:
        with pytest.raises(ValueError, match="clave"):
            await publicador.publicar("cualquiera", _evento(), clave="")


async def test_publicar_crudo_conserva_los_bytes_y_las_cabeceras(kafka_brokers, topic):
    async with PublicadorKafka(kafka_brokers, parametros_seguridad("PLAINTEXT"), client_id="prueba") as publicador:
        await publicador.publicar_crudo(topic, "k", b"no-es-json", {"error": "invalido"})

    (mensaje,) = await _leer(kafka_brokers, topic)

    assert mensaje.value == b"no-es-json" and dict(mensaje.headers)["error"] == b"invalido"


async def test_el_publicador_en_memoria_registra_lo_publicado_para_las_pruebas_unitarias():
    publicador = PublicadorMemoria()
    evento = _evento()

    await publicador.publicar("t", evento, clave="k")
    await publicador.publicar_crudo("t.dlq", "k", b"x", {"error": "e"})

    assert publicador.publicados == [("t", "k", evento)]
    assert publicador.crudos == [("t.dlq", "k", b"x", {"error": "e"})]
    assert json.loads(evento.a_bytes())["event_type"] == "cobro_prima_solicitado"
