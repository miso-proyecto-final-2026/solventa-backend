"""El relevo de la cola de salida contra un Kafka real: el evento llega con su clave y el sobre estándar."""

import json

import pytest
from app.eventos.conexion import parametros_seguridad
from app.eventos.publicador import PublicadorKafka
from app.suscripcion.config import TOPIC_COMANDOS
from app.suscripcion.relay import RelayEventos
from tests.eventos.test_publicador import _leer
from tests.test_servicio import _iniciar

pytestmark = pytest.mark.contenedores


async def test_la_decision_aprobada_llega_a_kafka_como_cobro_solicitado(armar, fabrica, kafka_brokers):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)

    async with PublicadorKafka(kafka_brokers, parametros_seguridad("PLAINTEXT"), client_id="prueba-suscripcion") as publicador:
        assert await RelayEventos(fabrica, publicador).publicar_pendientes() == 1

    mensajes = [m for m in await _leer(kafka_brokers, TOPIC_COMANDOS, cuantos=1) if m.key == susc.id.encode()]
    (mensaje,) = mensajes
    sobre = json.loads(mensaje.value)
    assert sobre["event_type"] == "cobro_prima_solicitado" and sobre["source"] == "ms-suscripcion"
    assert sobre["payload"]["suscripcion_id"] == susc.id and sobre["payload"]["metodo_pago"] == "tok_visa_123"
    assert dict(mensaje.headers)["event_type"] == b"cobro_prima_solicitado"
