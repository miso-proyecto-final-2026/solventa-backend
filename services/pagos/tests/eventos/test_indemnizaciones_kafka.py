"""De punta a punta contra un Kafka real (HU29, criterios 1, 4 y 8): comando → consumidor → pago → resultado en Kafka.

Se omite sola si Docker no está disponible.
"""

import asyncio
import json

import pytest
from app.eventos import ConsumidorEventos, PublicadorKafka, RegistroEventos
from app.eventos.conexion import parametros_seguridad
from app.pagos.config import GRUPO_INDEMNIZACIONES, TOPIC_INDEMNIZACIONES
from app.pagos.modelos_db import TABLA_EVENTOS_PROCESADOS, IndemnizacionDB
from app.pagos.relay import RelayEventos
from sqlalchemy import func, select
from tests.dobles import PasarelaFalsa, comando_indemnizacion, evento_indemnizacion
from tests.eventos.test_publicador import _leer

pytestmark = pytest.mark.contenedores
SEGURIDAD = parametros_seguridad("PLAINTEXT")


async def test_el_comando_se_paga_una_vez_y_el_resultado_llega_a_kafka(armar_indemnizaciones, fabrica, kafka_brokers, topic):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="prueba-indemnizaciones") as publicador:
        ev = evento_indemnizacion(comando_indemnizacion("evt-kafka", "pol-kafka"))
        await publicador.publicar(topic, ev, clave="evt-kafka")
        await publicador.publicar(topic, ev, clave="evt-kafka")  # reentrega del mismo evento
        consumidor = ConsumidorEventos(
            brokers=kafka_brokers, seguridad=SEGURIDAD, topic=topic, grupo=GRUPO_INDEMNIZACIONES,
            manejador=servicio.manejar_evento, fabrica_sesiones=fabrica,
            registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS), publicador=publicador,
        )
        await consumidor.iniciar()
        tarea = asyncio.create_task(consumidor.ejecutar())
        try:
            async with asyncio.timeout(60):
                while True:
                    with fabrica() as s:
                        if s.execute(select(func.count()).select_from(IndemnizacionDB)).scalar_one():
                            break
                    await asyncio.sleep(0.2)
            await asyncio.sleep(1)  # deja llegar la reentrega
        finally:
            await consumidor.detener()
            await asyncio.gather(tarea, return_exceptions=True)

        await servicio.procesar_pendientes()
        assert await RelayEventos(fabrica, publicador).publicar_pendientes() == 1

    with fabrica() as s:
        assert s.execute(select(func.count()).select_from(IndemnizacionDB)).scalar_one() == 1
    assert pasarela.cargos == 1
    mensajes = [m for m in await _leer(kafka_brokers, TOPIC_INDEMNIZACIONES, cuantos=1) if m.key == b"evt-kafka"]
    sobre = json.loads(mensajes[-1].value)
    assert sobre["event_type"] == "indemnizacion_pagada"
    assert (sobre["payload"]["evento_id"], sobre["payload"]["poliza_id"]) == ("evt-kafka", "pol-kafka")
