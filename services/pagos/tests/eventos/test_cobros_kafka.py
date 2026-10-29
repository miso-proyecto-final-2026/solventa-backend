"""De punta a punta contra un Kafka real (criterios 1, 3 y 7): comando → consumidor → cobro → resultado en Kafka.

Se omite sola si Docker no está disponible.
"""

import asyncio
import json

import pytest
from app.eventos import (
    ConsumidorEventos,
    PoliticaReintentos,
    PublicadorKafka,
    RegistroEventos,
)
from app.eventos.conexion import parametros_seguridad
from app.pagos.config import GRUPO_COBROS, TOPIC_RESULTADOS
from app.pagos.modelos_db import TABLA_EVENTOS_PROCESADOS, TransaccionDB
from app.pagos.relay import RelayEventos
from sqlalchemy import func, select
from tests.dobles import PasarelaFalsa, comando, evento
from tests.eventos.test_publicador import _leer

pytestmark = pytest.mark.contenedores
SEGURIDAD = parametros_seguridad("PLAINTEXT")


async def _crear_topic(brokers, nombre):
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    admin = AIOKafkaAdminClient(bootstrap_servers=brokers)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(nombre, num_partitions=1, replication_factor=1)])
    finally:
        await admin.close()


async def test_el_comando_se_cobra_una_vez_y_el_resultado_llega_a_kafka(armar, fabrica, kafka_brokers, topic):
    pasarela = PasarelaFalsa()
    servicio = armar(pasarela)
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="prueba-pagos") as publicador:
        ev = evento()
        await publicador.publicar(topic, ev, clave="susc-1")
        await publicador.publicar(topic, ev, clave="susc-1")  # reentrega del mismo evento
        consumidor = ConsumidorEventos(
            brokers=kafka_brokers, seguridad=SEGURIDAD, topic=topic, grupo=GRUPO_COBROS,
            manejador=servicio.manejar_evento, fabrica_sesiones=fabrica,
            registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS), publicador=publicador,
        )
        await consumidor.iniciar()
        tarea = asyncio.create_task(consumidor.ejecutar())
        try:
            async with asyncio.timeout(60):
                while True:
                    with fabrica() as s:
                        if s.execute(select(func.count()).select_from(TransaccionDB)).scalar_one():
                            break
                    await asyncio.sleep(0.2)
            await asyncio.sleep(1)  # deja llegar la reentrega
        finally:
            await consumidor.detener()
            await asyncio.gather(tarea, return_exceptions=True)

        await servicio.procesar_pendientes()
        assert await RelayEventos(fabrica, publicador).publicar_pendientes() == 1

    with fabrica() as s:
        assert s.execute(select(func.count()).select_from(TransaccionDB)).scalar_one() == 1
    assert pasarela.cargos == 1
    mensajes = [m for m in await _leer(kafka_brokers, TOPIC_RESULTADOS, cuantos=1) if m.key == b"susc-1"]
    sobre = json.loads(mensajes[-1].value)
    assert sobre["event_type"] == "cobro_prima_confirmado" and sobre["payload"]["suscripcion_id"] == "susc-1"


async def test_un_comando_invalido_se_reintenta_y_termina_en_la_cola_de_fallidos(armar, fabrica, kafka_brokers, topic):
    await _crear_topic(kafka_brokers, f"{topic}.dlq")
    esperas = []

    async def dormir(segundos):
        esperas.append(segundos)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="prueba-pagos-dlq") as publicador:
        await publicador.publicar(topic, evento(comando(monto="-1")), clave="susc-1")
        consumidor = ConsumidorEventos(
            brokers=kafka_brokers, seguridad=SEGURIDAD, topic=topic, grupo=GRUPO_COBROS,
            manejador=armar().manejar_evento, fabrica_sesiones=fabrica,
            registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS), publicador=publicador,
            politica=PoliticaReintentos(), dormir=dormir,
        )
        await consumidor.iniciar()
        tarea = asyncio.create_task(consumidor.ejecutar())
        try:
            fallidos = await _leer(kafka_brokers, f"{topic}.dlq", cuantos=1)
        finally:
            await consumidor.detener()
            await asyncio.gather(tarea, return_exceptions=True)

    assert esperas == [1, 2, 4, 8]  # 5 intentos: base 1 s, se duplica, tope 30 s
    assert dict(fallidos[0].headers)["intentos"] == b"5"
