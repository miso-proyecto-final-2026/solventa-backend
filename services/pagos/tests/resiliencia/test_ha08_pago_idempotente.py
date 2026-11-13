"""HA08 · Pago paramétrico idempotente por eventos (EC-D1.1): exactamente un pago por par evento–póliza.

Experimento contra un Kafka y un PostgreSQL reales (contenedores), con la pasarela simulada cuyo libro vive en la base:
`pasarela.pagos()` cuenta los pagos ejecutados de verdad. Inyecta reentregas, mata al consumidor a mitad del procesamiento,
reposiciona offsets, hace competir réplicas y falla la pasarela, y deja la evidencia cruda en
`$HA08_EVIDENCIA/evidencia-cruda.json` (si la variable existe). Informe: `docs/evidencias/HA08-pago-idempotente.md`.

Se omite sola si Docker no está disponible.
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from app.eventos import ConsumidorEventos, Evento, PublicadorKafka, RegistroEventos
from app.eventos.conexion import parametros_seguridad
from app.pagos.dominio import PasarelaNoDisponible
from app.pagos.indemnizaciones import ComandoIndemnizacion, ServicioIndemnizaciones
from app.pagos.modelos_db import SCHEMA, TABLA_EVENTOS_PROCESADOS
from app.pagos.simulador import ModoSimulador, PasarelaSimulada
from sqlalchemy import text

from tests.dobles import Reloj
from tests.eventos.conftest import kafka_brokers  # noqa: F401  (Kafka real de la sesión)
from tests.test_postgres import base_migrada, fabrica_pg  # noqa: F401  (fixtures de PostgreSQL migrado)

pytestmark = pytest.mark.contenedores
SEGURIDAD = parametros_seguridad("PLAINTEXT")
NAMESPACE = uuid.UUID("6f1c2f4e-5a53-4a0e-9c7b-1d6a9b1c2d3e")  # el mismo que usa Siniestros para el event_id del comando
PARTICIONES = 3
EVIDENCIA: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def _guardar_evidencia():
    yield
    destino = os.environ.get("HA08_EVIDENCIA")
    if destino:
        carpeta = Path(destino)
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "evidencia-cruda.json").write_text(json.dumps(EVIDENCIA, indent=2, ensure_ascii=False, sort_keys=True))


# --- utilidades --------------------------------------------------------------------------------------------------


def _par(i: int) -> tuple[str, str]:
    return f"evt-{i:04d}", f"pol-{i:04d}"


def _comando(i: int, *, nuevo_sobre: bool = False) -> Evento:
    """El comando de Siniestros: `event_id` derivado del par. Con `nuevo_sobre` simula un reenvío con otro sobre."""
    evento_id, poliza_id = _par(i)
    clave = f"{evento_id}:{poliza_id}"
    return Evento(
        event_id=str(uuid.uuid4()) if nuevo_sobre else str(uuid.uuid5(NAMESPACE, clave)),
        event_type="indemnizacion_solicitada",
        source="ms-siniestros",
        correlation_id=f"corr-{i}",
        payload={
            "idempotency_key": clave, "evento_id": evento_id, "poliza_id": poliza_id, "producto": "parametrico_clima",
            "monto_indemnizacion": 1_000_000 + i, "moneda": "COP", "regla": "sismo", "version_reglas": "ha08",
        },
    )


async def _crear_topic(brokers, nombre):
    admin = AIOKafkaAdminClient(bootstrap_servers=brokers)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(nombre, num_partitions=PARTICIONES, replication_factor=1)])
    finally:
        await admin.close()


def _uno(fabrica, sql):
    with fabrica() as s:
        return s.execute(text(sql)).scalar()


def _medir(fabrica, pasarela) -> dict:
    """Lo que dice la base: pagos creados y ejecutados, estados y reentregas de cada defensa."""
    return {
        "indemnizaciones_creadas": _uno(fabrica, f"SELECT count(*) FROM {SCHEMA}.indemnizaciones"),
        "pagadas": _uno(fabrica, f"SELECT count(*) FROM {SCHEMA}.indemnizaciones WHERE estado = 'PAGADA'"),
        "pagos_ejecutados_en_la_pasarela": pasarela.pagos(),
        "reentregas_del_consumidor": _uno(fabrica, f"SELECT coalesce(sum(reentregas), 0) FROM {SCHEMA}.eventos_procesados"),
        "reentregas_del_servicio": _uno(fabrica, f"SELECT coalesce(sum(reentregas), 0) FROM {SCHEMA}.indemnizaciones"),
        "intentos_de_pago": _uno(fabrica, f"SELECT count(*) FROM {SCHEMA}.intentos_pago"),
        "pares_distintos": _uno(fabrica, f"SELECT count(DISTINCT (evento_id, poliza_id)) FROM {SCHEMA}.indemnizaciones"),
    }


def _paso(texto: str) -> None:
    """Traza de la corrida (con `-s`): deja ver en qué etapa está si algo se detiene."""
    print(f"[HA08 {time.strftime('%H:%M:%S')}] {texto}", flush=True)


async def _esperar(condicion, *, segundos=90, que="la condición"):
    inicio = time.monotonic()
    _paso(f"esperando {que}")
    while not condicion():
        if time.monotonic() - inicio > segundos:
            raise AssertionError(f"Se agotó la espera de {que} ({segundos} s)")
        await asyncio.sleep(0.1)


def _servicio(fabrica, pasarela, reloj=None, **opciones):
    """Con el reloj real, salvo que el escenario lo controle: con uno fijo el retroceso exponencial nunca vence."""
    if reloj is not None:
        opciones["reloj"] = reloj
    return ServicioIndemnizaciones(
        fabrica=fabrica, pasarela=pasarela, timeout_pasarela_s=2, reintento_base_s=0.2, reintento_maximo_s=1, **opciones
    )


def _consumidor(brokers, fabrica, publicador, topic, grupo, servicio, manejador=None):
    return ConsumidorEventos(
        brokers=brokers, seguridad=SEGURIDAD, topic=topic, grupo=grupo,
        manejador=manejador or servicio.manejar_evento, fabrica_sesiones=fabrica,
        registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS), publicador=publicador,
    )


async def _estado_grupo(brokers, topic, grupo) -> dict:
    """Estado del grupo de consumo: offset confirmado, fin del log y retraso por partición."""
    from aiokafka import AIOKafkaConsumer, TopicPartition

    admin = AIOKafkaAdminClient(bootstrap_servers=brokers)
    await admin.start()
    try:
        confirmados = await admin.list_consumer_group_offsets(grupo)
    finally:
        await admin.close()
    lector = AIOKafkaConsumer(bootstrap_servers=brokers, group_id=None, **SEGURIDAD)
    await lector.start()
    try:
        particiones = [TopicPartition(topic, p) for p in range(PARTICIONES)]
        fines = await lector.end_offsets(particiones)
    finally:
        await lector.stop()
    estado = {}
    for tp in particiones:
        confirmado = confirmados[tp].offset if tp in confirmados else 0
        estado[str(tp.partition)] = {"confirmado": confirmado, "fin": fines[tp], "retraso": fines[tp] - confirmado}
    return estado


async def _publicar(publicador, topic, evento, i):
    await publicador.publicar(topic, evento, clave=_par(i)[0])


async def _detener(consumidor: ConsumidorEventos) -> None:
    """Cierra el consumidor con tope: un cierre colgado no debe tapar el resultado del escenario."""
    try:
        await asyncio.wait_for(consumidor.detener(), 30)
    except TimeoutError:
        _paso("el consumidor no cerró a tiempo")


class _Carrera:
    """Arranca consumidores y procesadores y los detiene al final, pase lo que pase."""

    def __init__(self):
        self.tareas: list[asyncio.Task] = []
        self.consumidores: list[ConsumidorEventos] = []

    async def consumidor(self, c: ConsumidorEventos):
        await c.iniciar()
        self.consumidores.append(c)
        self.tareas.append(asyncio.create_task(c.ejecutar()))

    def procesador(self, servicio: ServicioIndemnizaciones):
        self.tareas.append(asyncio.create_task(servicio.ejecutar(intervalo_s=0.05)))

    async def detener(self):
        for c in self.consumidores:
            await _detener(c)
        for t in self.tareas:
            t.cancel()
        await asyncio.gather(*self.tareas, return_exceptions=True)


# --- criterios 1 y 2: un pago por par; cada reentrega registrada y sin volver a pagar -------------------------------

PARES = 50
COPIAS_MISMO_SOBRE = 3  # reentregas del bus: mismo event_id
COPIAS_OTRO_SOBRE = 1   # reenvíos con otro sobre: misma idempotency_key, otro event_id


async def test_ha08_1_y_2_reentregas_no_duplican_pagos_y_quedan_registradas(fabrica_pg, kafka_brokers):  # noqa: F811
    topic, grupo = f"ha08.{uuid.uuid4().hex[:8]}", "solventa-pagos.ha08-reentregas"
    _paso("creando el topic")
    await _crear_topic(kafka_brokers, topic)
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    servicio = _servicio(fabrica_pg, pasarela)
    carrera = _Carrera()
    enviados = 0
    inicio = time.monotonic()
    _paso("iniciando el publicador")
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-1") as publicador:
        _paso("publicando")
        for i in range(PARES):
            for _ in range(1 + COPIAS_MISMO_SOBRE):
                await _publicar(publicador, topic, _comando(i), i)
                enviados += 1
            for _ in range(COPIAS_OTRO_SOBRE):
                await _publicar(publicador, topic, _comando(i, nuevo_sobre=True), i)
                enviados += 1
        _paso(f"publicados {enviados} mensajes; iniciando el consumidor")
        await carrera.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio))
        carrera.procesador(servicio)
        try:
            esperadas = PARES * (COPIAS_MISMO_SOBRE + COPIAS_OTRO_SOBRE)
            await _esperar(
                lambda: (m := _medir(fabrica_pg, pasarela))["pagadas"] == PARES
                and m["reentregas_del_consumidor"] + m["reentregas_del_servicio"] == esperadas,
                que="que todas las entregas se procesen",
            )
        finally:
            await carrera.detener()
    medida = _medir(fabrica_pg, pasarela)
    EVIDENCIA["1_2_reentregas"] = {
        "pares_distintos_enviados": PARES, "mensajes_enviados": enviados,
        "duplicados_inyectados": enviados - PARES, "duplicados_con_mismo_sobre": PARES * COPIAS_MISMO_SOBRE,
        "duplicados_con_otro_sobre": PARES * COPIAS_OTRO_SOBRE, "segundos": round(time.monotonic() - inicio, 2),
        **medida, "grupo": await _estado_grupo(kafka_brokers, topic, grupo),
    }
    assert medida["pagos_ejecutados_en_la_pasarela"] == PARES == medida["pares_distintos"] == medida["indemnizaciones_creadas"]
    assert medida["reentregas_del_consumidor"] == PARES * COPIAS_MISMO_SOBRE
    assert medida["reentregas_del_servicio"] == PARES * COPIAS_OTRO_SOBRE
    assert medida["intentos_de_pago"] == PARES  # una sola orden a la pasarela por par

    # --- criterio 4: reposicionar los offsets y releer todo el topic no produce pagos nuevos --------------------
    antes = _medir(fabrica_pg, pasarela)
    consumidor = _consumidor(kafka_brokers, fabrica_pg, None, topic, grupo, servicio)
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-4") as publicador:
        consumidor._publicador = publicador
        await consumidor.iniciar()
        # Reposicionar antes de que el grupo asigne particiones no hace nada: se espera la asignación primero.
        async with asyncio.timeout(60):
            while not consumidor._consumidor.assignment():
                await consumidor._consumidor.getmany(timeout_ms=200, max_records=0)
        _paso(f"particiones asignadas: {sorted(tp.partition for tp in consumidor._consumidor.assignment())}; reposicionando offsets")
        await consumidor._consumidor.seek_to_beginning()
        tarea = asyncio.create_task(consumidor.ejecutar())
        try:
            await _esperar(
                lambda: _medir(fabrica_pg, pasarela)["reentregas_del_consumidor"] >= antes["reentregas_del_consumidor"] + enviados - PARES * COPIAS_OTRO_SOBRE,
                que="la relectura completa del topic",
            )
        finally:
            await _detener(consumidor)
            await asyncio.gather(tarea, return_exceptions=True)
    despues = _medir(fabrica_pg, pasarela)
    EVIDENCIA["4_reposicion_de_offsets"] = {
        "mensajes_releidos": enviados, "antes": antes, "despues": despues,
        "grupo": await _estado_grupo(kafka_brokers, topic, grupo),
    }
    assert despues["pagos_ejecutados_en_la_pasarela"] == antes["pagos_ejecutados_en_la_pasarela"] == PARES
    assert despues["indemnizaciones_creadas"] == PARES and despues["intentos_de_pago"] == PARES


# --- criterio 3: caída del consumidor ---------------------------------------------------------------------------

CAIDA_PARES = 12


async def test_ha08_3a_el_consumidor_muere_antes_de_confirmar_la_base_no_pierde_ni_duplica(fabrica_pg, kafka_brokers):  # noqa: F811
    """Muere con la transacción abierta: la base se deshace, el offset no se confirma y al volver se procesa una vez."""
    topic, grupo = f"ha08.{uuid.uuid4().hex[:8]}", "solventa-pagos.ha08-caida-a"
    await _crear_topic(kafka_brokers, topic)
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    servicio = _servicio(fabrica_pg, pasarela)
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-3a") as publicador:
        for i in range(CAIDA_PARES):
            await _publicar(publicador, topic, _comando(i), i)

        iniciado = asyncio.Event()

        async def manejador_que_muere(evento, sesion):
            await servicio.manejar_evento(evento, sesion)  # ya reservó y registró, pero nada está confirmado
            iniciado.set()
            await asyncio.sleep(3600)

        primero = _Carrera()
        await primero.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio, manejador_que_muere))
        await asyncio.wait_for(iniciado.wait(), 60)
        primero.tareas[0].cancel()  # el consumidor muere a mitad del procesamiento
        await asyncio.gather(*primero.tareas, return_exceptions=True)
        await primero.detener()
        tras_la_muerte = _medir(fabrica_pg, pasarela)
        assert tras_la_muerte["indemnizaciones_creadas"] == 0  # la transacción se deshizo: nada a medias

        segundo = _Carrera()
        await segundo.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio))
        segundo.procesador(servicio)
        try:
            await _esperar(lambda: _medir(fabrica_pg, pasarela)["pagadas"] == CAIDA_PARES, que="la recuperación")
        finally:
            await segundo.detener()
    final = _medir(fabrica_pg, pasarela)
    EVIDENCIA["3a_muerte_antes_de_confirmar"] = {
        "pares": CAIDA_PARES, "tras_la_muerte": tras_la_muerte, "tras_la_recuperacion": final, "reinicios_del_consumidor": 1,
        "grupo": await _estado_grupo(kafka_brokers, topic, grupo),
    }
    assert final["pagos_ejecutados_en_la_pasarela"] == CAIDA_PARES == final["pares_distintos"]


async def test_ha08_3b_el_consumidor_muere_despues_de_confirmar_la_base_y_antes_del_offset(fabrica_pg, kafka_brokers):  # noqa: F811
    """Peor momento: la base ya confirmó y el offset no. Al volver, el mensaje se reentrega y se omite (reentrega registrada)."""
    topic, grupo = f"ha08.{uuid.uuid4().hex[:8]}", "solventa-pagos.ha08-caida-b"
    await _crear_topic(kafka_brokers, topic)
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    servicio = _servicio(fabrica_pg, pasarela)
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-3b") as publicador:
        for i in range(CAIDA_PARES):
            await _publicar(publicador, topic, _comando(i), i)

        primero = _consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio)
        await primero.iniciar()

        async def commit_que_muere(_offsets):
            raise asyncio.CancelledError  # el proceso cae justo después de confirmar la base

        primero._consumidor.commit = commit_que_muere
        tarea = asyncio.create_task(primero.ejecutar())
        await asyncio.gather(tarea, return_exceptions=True)  # termina al primer commit
        await primero.detener()
        tras_la_muerte = _medir(fabrica_pg, pasarela)
        assert tras_la_muerte["indemnizaciones_creadas"] >= 1  # la base sí confirmó

        segundo = _Carrera()
        await segundo.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio))
        segundo.procesador(servicio)
        try:
            await _esperar(lambda: _medir(fabrica_pg, pasarela)["pagadas"] == CAIDA_PARES, que="la recuperación")
        finally:
            await segundo.detener()
    final = _medir(fabrica_pg, pasarela)
    EVIDENCIA["3b_muerte_despues_de_confirmar"] = {
        "pares": CAIDA_PARES, "tras_la_muerte": tras_la_muerte, "tras_la_recuperacion": final, "reinicios_del_consumidor": 1,
        "grupo": await _estado_grupo(kafka_brokers, topic, grupo),
    }
    assert final["pagos_ejecutados_en_la_pasarela"] == CAIDA_PARES == final["pares_distintos"]
    assert final["reentregas_del_consumidor"] >= 1  # el mensaje reentregado quedó registrado


async def test_ha08_3c_el_procesador_muere_despues_de_que_la_pasarela_pago(fabrica_pg, kafka_brokers):  # noqa: F811
    """El peor caso del pago: la pasarela cobró y el proceso cae antes de anotarlo. Al volver se concilia sin pagar otra vez."""
    reloj = Reloj()
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    servicio = _servicio(fabrica_pg, pasarela, reloj)
    with fabrica_pg() as s, s.begin():
        servicio.registrar(s, ComandoIndemnizacion.desde_evento(_comando(0).payload))
    ind_id = _uno(fabrica_pg, f"SELECT id FROM {SCHEMA}.indemnizaciones")

    original = servicio._cerrar_intento
    def morir(*_a, **_k):
        raise SystemExit("el proceso cae después de que la pasarela pagó")
    servicio._cerrar_intento = morir
    with pytest.raises(SystemExit):
        await servicio.procesar(ind_id)
    servicio._cerrar_intento = original
    tras_la_muerte = _medir(fabrica_pg, pasarela)
    assert tras_la_muerte["pagos_ejecutados_en_la_pasarela"] == 1 and tras_la_muerte["pagadas"] == 0  # pagó y nadie lo sabe

    reloj.avanzar(60)  # vence el préstamo de la réplica caída
    await servicio.procesar(ind_id)
    final = _medir(fabrica_pg, pasarela)
    EVIDENCIA["3c_muerte_del_procesador_tras_pagar"] = {
        "tras_la_muerte": tras_la_muerte, "tras_la_recuperacion": final,
        "intentos": _uno(fabrica_pg, f"SELECT string_agg(resultado, ',' ORDER BY numero) FROM {SCHEMA}.intentos_pago"),
    }
    assert final["pagos_ejecutados_en_la_pasarela"] == 1 and final["pagadas"] == 1


# --- criterio 5: concurrencia ------------------------------------------------------------------------------------

REPLICAS = 3
CONCURRENCIA_PARES = 30


async def test_ha08_5_varias_replicas_procesando_los_mismos_comandos_producen_un_pago_por_par(fabrica_pg, kafka_brokers):  # noqa: F811
    """Cada réplica lee el mismo flujo (grupos distintos) y procesa a la vez: la base decide quién gana."""
    topic = f"ha08.{uuid.uuid4().hex[:8]}"
    await _crear_topic(kafka_brokers, topic)
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    carrera = _Carrera()
    inicio = time.monotonic()
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-5") as publicador:
        for i in range(CONCURRENCIA_PARES):
            await _publicar(publicador, topic, _comando(i), i)
        for r in range(REPLICAS):
            servicio = _servicio(fabrica_pg, pasarela)
            await carrera.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, f"solventa-pagos.ha08-replica-{r}", servicio))
            carrera.procesador(servicio)
        try:
            await _esperar(
                lambda: _medir(fabrica_pg, pasarela)["pagadas"] == CONCURRENCIA_PARES
                and _uno(fabrica_pg, f"SELECT count(*) FROM {SCHEMA}.eventos_procesados") == CONCURRENCIA_PARES * REPLICAS,
                que="que las réplicas terminen",
            )
        finally:
            await carrera.detener()
    medida = _medir(fabrica_pg, pasarela)
    EVIDENCIA["5_concurrencia"] = {
        "replicas": REPLICAS, "pares": CONCURRENCIA_PARES, "lecturas_totales": CONCURRENCIA_PARES * REPLICAS,
        "segundos": round(time.monotonic() - inicio, 2), **medida,
    }
    assert medida["pagos_ejecutados_en_la_pasarela"] == CONCURRENCIA_PARES == medida["indemnizaciones_creadas"]
    assert medida["reentregas_del_servicio"] == CONCURRENCIA_PARES * (REPLICAS - 1)  # cada par lo vio REPLICAS veces


# --- criterio 6: fallo transitorio de la pasarela ----------------------------------------------------------------


class _PasarelaIntermitente:
    """Falla las primeras `fallos` órdenes de cada clave (la pasarela no recibe nada) y luego delega en el simulador."""

    def __init__(self, real: PasarelaSimulada, fallos: int) -> None:
        self._real, self._fallos, self.rechazadas = real, fallos, 0
        self._vistas: dict[str, int] = {}

    async def pagar(self, solicitud):
        n = self._vistas[solicitud.idempotency_key] = self._vistas.get(solicitud.idempotency_key, 0) + 1
        if n <= self._fallos:
            self.rechazadas += 1
            raise PasarelaNoDisponible("falla transitoria inyectada")
        return await self._real.pagar(solicitud)

    async def consultar(self, clave):
        return await self._real.consultar(clave)


async def test_ha08_6_un_fallo_transitorio_se_reintenta_con_la_misma_clave_sin_segundo_pago(fabrica_pg, kafka_brokers):  # noqa: F811
    topic, grupo = f"ha08.{uuid.uuid4().hex[:8]}", "solventa-pagos.ha08-transitorio"
    await _crear_topic(kafka_brokers, topic)
    real = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    pasarela = _PasarelaIntermitente(real, fallos=2)
    servicio = _servicio(fabrica_pg, pasarela)
    carrera = _Carrera()
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-6") as publicador:
        for i in range(20):
            await _publicar(publicador, topic, _comando(i), i)
        await carrera.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio))
        carrera.procesador(servicio)
        try:
            await _esperar(lambda: _medir(fabrica_pg, real)["pagadas"] == 20, que="que los reintentos terminen")
        finally:
            await carrera.detener()
    medida = _medir(fabrica_pg, real)
    EVIDENCIA["6_fallo_transitorio"] = {"pares": 20, "fallas_inyectadas": pasarela.rechazadas, **medida}
    assert pasarela.rechazadas == 40  # dos por cada par
    assert medida["pagos_ejecutados_en_la_pasarela"] == 20 and medida["intentos_de_pago"] == 60  # 3 órdenes por par, 1 pago


# --- criterio 7: disponibilidad en la corrida --------------------------------------------------------------------


async def test_ha08_7_disponibilidad_del_flujo_de_pago_en_la_corrida(fabrica_pg, kafka_brokers):  # noqa: F811
    """Indicador de corrida corta (a confirmar): comandos resueltos dentro de un plazo, sobre los comandos enviados."""
    topic, grupo = f"ha08.{uuid.uuid4().hex[:8]}", "solventa-pagos.ha08-disponibilidad"
    await _crear_topic(kafka_brokers, topic)
    pasarela = PasarelaSimulada(fabrica_pg, modo=ModoSimulador.APROBAR)
    servicio = _servicio(fabrica_pg, pasarela)
    carrera = _Carrera()
    total, plazo_s = 100, 5.0
    async with PublicadorKafka(kafka_brokers, SEGURIDAD, client_id="ha08-7") as publicador:
        await carrera.consumidor(_consumidor(kafka_brokers, fabrica_pg, publicador, topic, grupo, servicio))
        carrera.procesador(servicio)
        try:
            for i in range(total):
                await _publicar(publicador, topic, _comando(i), i)
                await asyncio.sleep(0.02)
            await _esperar(lambda: _medir(fabrica_pg, pasarela)["pagadas"] == total, que="las 100 indemnizaciones")
        finally:
            await carrera.detener()
    with fabrica_pg() as sesion:
        latencias = [
            round(float(x), 3)
            for x in sesion.execute(
                text(f"SELECT extract(epoch FROM (resuelta_en - created_at)) FROM {SCHEMA}.indemnizaciones ORDER BY 1")
            ).scalars()
        ]
    medida = _medir(fabrica_pg, pasarela)
    en_plazo = _uno(
        fabrica_pg, f"SELECT count(*) FROM {SCHEMA}.indemnizaciones WHERE estado = 'PAGADA' AND resuelta_en - created_at <= interval '{plazo_s} seconds'"
    )
    EVIDENCIA["7_disponibilidad"] = {
        "comandos_enviados": total, "plazo_s": plazo_s, "resueltos_en_plazo": en_plazo, "pagadas": medida["pagadas"],
        "disponibilidad_pct": round(100 * en_plazo / total, 4), "errores_no_controlados": total - medida["pagadas"], "latencias_s": latencias,
    }
    assert en_plazo == total
