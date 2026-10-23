"""De punta a punta contra un Kafka real (criterios 5, 6 y 7): recepción → Kafka → consumidor → procesado.

Se omite sola si Docker no está disponible.
"""

import asyncio

import pytest

from app.eventos import (
    ConsumidorEventos,
    PublicadorKafka,
    RegistroEventos,
    parametros_seguridad,
)
from app.siniestros.modelos_db import EVENTOS_PROCESADOS
from app.siniestros.servicio import (
    GRUPO_RECEPCION,
    TOPIC_EVENTOS,
    Receptor,
    procesar_evento,
)
from tests.siniestros.conftest import AHORA, FUENTE, Entorno, evento

pytestmark = pytest.mark.contenedores
SEGURIDAD = parametros_seguridad("PLAINTEXT")


def _consumidor(brokers, entorno, publicador) -> ConsumidorEventos:
    return ConsumidorEventos(
        brokers=brokers,
        seguridad=SEGURIDAD,
        topic=TOPIC_EVENTOS,
        grupo=GRUPO_RECEPCION,
        manejador=procesar_evento,
        fabrica_sesiones=entorno.fabrica,
        registro=RegistroEventos(EVENTOS_PROCESADOS),
        publicador=publicador,
    )


async def _hasta_procesados(receptor: Receptor, cuantos: int, limite: float = 60) -> dict[str, int]:
    async with asyncio.timeout(limite):
        while (resumen := await receptor.resumen())["procesados"] < cuantos:
            await asyncio.sleep(0.2)
    return resumen


async def test_la_rafaga_se_acepta_con_el_consumidor_caido_y_se_drena_sin_perder_ni_duplicar(
    kafka_brokers, tmp_path
):
    entorno = Entorno(url=f"sqlite:///{tmp_path / 'siniestros.db'}")
    try:
        async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="prueba-siniestros") as publicador:
            receptor = Receptor(entorno.repo, publicador, reloj=lambda: AHORA)

            # 1) Consumidor caído: 30 eventos distintos, cada uno enviado dos veces (el proveedor reintenta).
            cuerpos = [evento(evento_id=f"e-{i}", poliza_id=f"pol-{i % 5}") for i in range(30)]
            acuses = await asyncio.gather(*(receptor.recibir(c, FUENTE) for c in cuerpos * 2))
            assert sum(1 for a in acuses if not a.duplicado) == 30
            resumen = await receptor.resumen()
            assert resumen["recibidos"] == 30 and resumen["publicados"] == 30 and resumen["procesados"] == 0

            # 2) El consumidor arranca y drena el rezago acumulado.
            primero = _consumidor(kafka_brokers, entorno, publicador)
            await primero.iniciar()
            tarea = asyncio.create_task(primero.ejecutar())
            try:
                await _hasta_procesados(receptor, 30)
            finally:
                await primero.detener()
                await asyncio.gather(tarea, return_exceptions=True)

            # 3) Cae de nuevo; llegan 10 eventos más. Al volver continúa donde quedó, sin repetir.
            await asyncio.gather(
                *(receptor.recibir(evento(evento_id=f"n-{i}", poliza_id="pol-9"), FUENTE) for i in range(10))
            )
            segundo = _consumidor(kafka_brokers, entorno, publicador)
            await segundo.iniciar()
            tarea = asyncio.create_task(segundo.ejecutar())
            try:
                resumen = await _hasta_procesados(receptor, 40)
            finally:
                await segundo.detener()
                await asyncio.gather(tarea, return_exceptions=True)

        assert resumen == {"recibidos": 40, "rechazados": 0, "publicados": 40, "procesados": 40, "reentregas": 0}
    finally:
        entorno.engine.dispose()
