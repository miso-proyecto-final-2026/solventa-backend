"""Consumidor idempotente: reentregas sin repetir trabajo, reintentos con retroceso, cola de fallidos y confirmación tras procesar."""

import asyncio

import pytest
from aiokafka import AIOKafkaConsumer
from eventos import Evento
from eventos.conexion import parametros_seguridad
from eventos.consumidor import ConsumidorEventos, PoliticaReintentos
from eventos.publicador import PublicadorKafka
from sqlalchemy import func, insert, select

pytestmark = pytest.mark.contenedores
SEGURIDAD = parametros_seguridad("PLAINTEXT")


def _evento(**cambios):
    datos = {"event_type": "cobro_prima_solicitado", "source": "ms-suscripcion", "correlation_id": "saga-1", "payload": {"monto": 1}}
    return Evento(**{**datos, **cambios})


class Esperas:
    """Sustituye a asyncio.sleep para medir el retroceso sin esperar de verdad."""

    def __init__(self):
        self.valores = []

    async def __call__(self, segundos):
        self.valores.append(segundos)


def _consumidor(brokers, base, topic, grupo, manejador, publicador, *, politica=None, dormir=None):
    return ConsumidorEventos(
        brokers=brokers,
        seguridad=SEGURIDAD,
        topic=topic,
        grupo=grupo,
        manejador=manejador,
        fabrica_sesiones=base.fabrica,
        registro=base.registro,
        publicador=publicador,
        politica=politica or PoliticaReintentos(),
        dormir=dormir or Esperas(),
    )


async def _publicar(brokers, topic, *eventos):
    async with PublicadorKafka(brokers, SEGURIDAD, client_id="prueba") as publicador:
        for e in eventos:
            await publicador.publicar(topic, e, clave="k")
        return publicador


async def _correr(consumidor, hasta, limite=40):
    """Ejecuta el consumidor hasta que `hasta()` sea verdadero (o venza el límite) y lo detiene."""
    await consumidor.iniciar()
    tarea = asyncio.create_task(consumidor.ejecutar())
    try:
        async with asyncio.timeout(limite):
            while not hasta():
                if tarea.done():
                    tarea.result()
                await asyncio.sleep(0.05)
    finally:
        await consumidor.detener()
        await asyncio.gather(tarea, return_exceptions=True)


async def _correr_durante(consumidor, segundos):
    await consumidor.iniciar()
    tarea = asyncio.create_task(consumidor.ejecutar())
    await asyncio.sleep(segundos)
    await consumidor.detener()
    await tarea


async def _leer(brokers, topic, cuantos=1):
    lector = AIOKafkaConsumer(topic, bootstrap_servers=brokers, group_id=None, auto_offset_reset="earliest", **SEGURIDAD)
    await lector.start()
    try:
        mensajes = []
        async with asyncio.timeout(30):
            while len(mensajes) < cuantos:
                for registros in (await lector.getmany(timeout_ms=1000)).values():
                    mensajes.extend(registros)
        return mensajes
    finally:
        await lector.stop()


def _reentregas(base, grupo, event_id):
    with base.fabrica() as s:
        return s.execute(
            select(base.tabla.c.reentregas).where(base.tabla.c.consumidor == grupo, base.tabla.c.event_id == event_id)
        ).scalar_one_or_none()


def _efectos(base):
    with base.fabrica() as s:
        return s.execute(select(func.count()).select_from(base.efectos)).scalar_one()


async def test_entrega_el_evento_al_manejador_y_confirma_el_offset(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)
    recibidos = []

    async def manejador(e, sesion):
        recibidos.append(e)

    publicador = PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq")
    await publicador.iniciar()
    try:
        await _correr(_consumidor(kafka_brokers, base, topic, "svc-a", manejador, publicador), lambda: recibidos)
        assert recibidos == [evento]

        # Otro consumidor del mismo grupo no recibe nada: el offset quedó confirmado.
        otros = []

        async def otro(e, sesion):
            otros.append(e)

        await _correr_durante(_consumidor(kafka_brokers, base, topic, "svc-a", otro, publicador), 3)
        assert otros == []
    finally:
        await publicador.detener()


async def test_una_reentrega_no_repite_el_trabajo_y_queda_registrada(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento, evento)  # Kafka entrega al menos una vez: puede repetir
    llamadas = []

    async def manejador(e, sesion):
        llamadas.append(e.event_id)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr(
            _consumidor(kafka_brokers, base, topic, "svc-a", manejador, publicador),
            lambda: _reentregas(base, "svc-a", evento.event_id) == 1,
        )

    assert llamadas == [evento.event_id]


async def test_cada_grupo_procesa_el_evento_una_sola_vez(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)
    a, b = [], []

    async def maneja_a(e, sesion):
        a.append(e)

    async def maneja_b(e, sesion):
        b.append(e)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await asyncio.gather(
            _correr(_consumidor(kafka_brokers, base, topic, "svc-a", maneja_a, publicador), lambda: a),
            _correr(_consumidor(kafka_brokers, base, topic, "svc-b", maneja_b, publicador), lambda: b),
        )

    assert len(a) == 1 and len(b) == 1


async def test_reintenta_con_retroceso_exponencial_y_se_recupera(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)
    intentos = []
    esperas = Esperas()

    async def manejador(e, sesion):
        intentos.append(1)
        if len(intentos) < 3:
            raise RuntimeError("falla transitoria")

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr(
            _consumidor(kafka_brokers, base, topic, "svc-a", manejador, publicador, dormir=esperas),
            lambda: _reentregas(base, "svc-a", evento.event_id) == 0,
        )

    assert len(intentos) == 3
    assert esperas.valores == [1.0, 2.0]


async def test_agotados_los_reintentos_el_evento_va_a_la_cola_de_fallidos_y_no_se_repite(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)
    intentos = []
    esperas = Esperas()

    async def manejador(e, sesion):
        intentos.append(1)
        raise RuntimeError("siempre falla")

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr(
            _consumidor(
                kafka_brokers, base, topic, "svc-a", manejador, publicador, politica=PoliticaReintentos(intentos=3), dormir=esperas
            ),
            lambda: len(intentos) >= 3,
        )
        # Espera a que el evento haya llegado a la cola de fallidos antes de revisar.
        (fallido,) = await _leer(kafka_brokers, f"{topic}.dlq")

    cabeceras = {k: v.decode() for k, v in fallido.headers}
    assert len(intentos) == 3 and esperas.valores == [1.0, 2.0]
    assert fallido.value == evento.a_bytes()
    assert cabeceras["intentos"] == "3" and cabeceras["grupo"] == "svc-a" and "siempre falla" in cabeceras["error"]
    assert cabeceras["topic_origen"] == topic
    assert _reentregas(base, "svc-a", evento.event_id) is None  # no quedó marcado como procesado

    # Su offset se confirmó: un consumidor nuevo del grupo no lo recibe otra vez.
    nuevos = []

    async def otro(e, sesion):
        nuevos.append(e)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr_durante(_consumidor(kafka_brokers, base, topic, "svc-a", otro, publicador), 3)
    assert nuevos == []


async def test_un_mensaje_ilegible_va_directo_a_la_cola_de_fallidos_sin_reintentos(kafka_brokers, base, topic):
    esperas = Esperas()
    llamadas = []

    async def manejador(e, sesion):
        llamadas.append(e)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await publicador.publicar_crudo(topic, "k", b"esto no es un sobre", {})
        consumidor = _consumidor(kafka_brokers, base, topic, "svc-a", manejador, publicador, dormir=esperas)
        await consumidor.iniciar()
        tarea = asyncio.create_task(consumidor.ejecutar())
        try:
            (fallido,) = await _leer(kafka_brokers, f"{topic}.dlq")
        finally:
            await consumidor.detener()
            await tarea

    assert fallido.value == b"esto no es un sobre"
    assert "EventoInvalido" in dict(fallido.headers)["error"].decode()
    assert llamadas == [] and esperas.valores == []


async def test_el_trabajo_de_un_intento_fallido_se_deshace(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)
    intentos = []

    async def manejador(e, sesion):
        sesion.execute(insert(base.efectos).values(valor="efecto"))
        intentos.append(1)
        if len(intentos) == 1:
            raise RuntimeError("falla después de escribir")

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr(_consumidor(kafka_brokers, base, topic, "svc-a", manejador, publicador), lambda: len(intentos) >= 2)
        await asyncio.sleep(0.5)

    assert len(intentos) == 2
    assert _efectos(base) == 1  # el efecto del primer intento se deshizo; solo queda el del segundo


async def test_si_falla_la_cola_de_fallidos_no_se_confirma_y_el_evento_se_reentrega(kafka_brokers, base, topic):
    evento = _evento()
    await _publicar(kafka_brokers, topic, evento)

    async def siempre_falla(e, sesion):
        raise RuntimeError("falla")

    class PublicadorRoto:
        async def publicar(self, *a, **k):
            raise ConnectionError("sin bus")

        async def publicar_crudo(self, *a, **k):
            raise ConnectionError("sin bus")

    roto = _consumidor(
        kafka_brokers, base, topic, "svc-a", siempre_falla, PublicadorRoto(), politica=PoliticaReintentos(intentos=1)
    )
    await roto.iniciar()
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(roto.ejecutar(), timeout=30)
    finally:
        await roto.detener()

    recibidos = []

    async def bien(e, sesion):
        recibidos.append(e)

    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="dlq") as publicador:
        await _correr(_consumidor(kafka_brokers, base, topic, "svc-a", bien, publicador), lambda: recibidos)

    assert recibidos == [evento]


def test_el_retroceso_duplica_y_tiene_tope():
    p = PoliticaReintentos()

    assert [p.espera(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert p.intentos == 5


@pytest.mark.parametrize("campo", ["intentos", "base_s", "maximo_s"])
def test_la_politica_exige_valores_positivos(campo):
    with pytest.raises(ValueError):
        PoliticaReintentos(**{campo: 0})
