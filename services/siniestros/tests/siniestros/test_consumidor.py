"""Consumidor: reintentos con retroceso, cola de fallidos, idempotencia y recuperación (criterio 7)."""

import asyncio

from app.siniestros.bus import (
    TOPIC_EVENTOS,
    TOPIC_EVENTOS_FALLIDOS,
    BusMemoria,
    Consumidor,
)
from tests.siniestros.conftest import AHORA, evento, no_dormir

G = "g"


def _consumidor(bus, manejador, **kw):
    return Consumidor(bus, TOPIC_EVENTOS, G, manejador, dormir=kw.pop("dormir", no_dormir), **kw)


async def test_un_fallo_transitorio_se_reintenta_con_retroceso_exponencial():
    bus = BusMemoria(particiones=1)
    await bus.publicar(TOPIC_EVENTOS, "k", {"n": 1})
    intentos, esperas = [], []

    async def manejador(_m):
        intentos.append(1)
        if len(intentos) < 3:
            raise RuntimeError("transitorio")

    async def dormir(s):
        esperas.append(s)

    await _consumidor(bus, manejador, reintentos=3, espera_base_s=0.5, dormir=dormir).drenar()

    assert len(intentos) == 3
    assert esperas == [0.5, 1.0]
    assert await bus.rezago(TOPIC_EVENTOS, G) == 0


async def test_agotados_los_reintentos_va_a_la_cola_de_fallidos_sin_frenar_el_resto():
    bus = BusMemoria(particiones=1)
    await bus.publicar(TOPIC_EVENTOS, "k", {"n": 1})
    await bus.publicar(TOPIC_EVENTOS, "k", {"n": 2})
    vistos = []

    async def manejador(m):
        if m.valor["n"] == 1:
            raise RuntimeError("veneno")
        vistos.append(m.valor["n"])

    await _consumidor(bus, manejador, reintentos=2).drenar()

    assert vistos == [2]
    [fallido] = await bus.leer(TOPIC_EVENTOS_FALLIDOS, "dlq", 10)
    assert fallido.valor["n"] == 1 and fallido.valor["_origen"] == TOPIC_EVENTOS


async def test_si_el_consumidor_cae_el_rezago_se_acumula_y_al_volver_se_drena_completo(entorno):
    # El consumidor está caído mientras llegan 50 eventos: el acuse sigue siendo inmediato.
    for i in range(50):
        assert entorno.enviar(evento(evento_id=f"e-{i}")).status_code == 202
    assert entorno.resumen()["rezago"] == 50 and entorno.resumen()["procesados"] == 0

    await entorno.consumidor.drenar()  # el consumidor se recupera

    resumen = entorno.resumen()
    assert resumen["procesados"] == 50 and resumen["rezago"] == 0 and resumen["reentregas"] == 0


async def test_una_reentrega_no_duplica_el_procesamiento_y_queda_registrada(entorno):
    entorno.enviar()
    [m] = await entorno.bus.leer(TOPIC_EVENTOS, "grupo-auxiliar", 10)
    manejador = entorno.consumidor._manejador

    await manejador(m)
    await manejador(m)  # el bus reentrega el mismo mensaje

    resumen = entorno.resumen()
    assert resumen["procesados"] == 1 and resumen["reentregas"] == 1


async def test_la_caida_a_mitad_de_lote_reentrega_lo_no_confirmado_sin_duplicar(entorno):
    for i in range(4):
        entorno.enviar(evento(evento_id=f"e-{i}"))
    cayo = {"ya": False}
    original = entorno.consumidor._manejador

    async def se_cae_en_el_tercero(m):
        await original(m)
        if not cayo["ya"] and entorno.resumen()["procesados"] == 2:
            cayo["ya"] = True
            raise asyncio.CancelledError  # el proceso muere antes de confirmar

    entorno.consumidor._manejador = se_cae_en_el_tercero
    try:
        await entorno.consumidor.drenar()
    except asyncio.CancelledError:
        pass
    entorno.consumidor._manejador = original

    await entorno.consumidor.drenar()  # reinicio: continúa desde lo último confirmado

    resumen = entorno.resumen()
    assert resumen["procesados"] == 4 and resumen["rezago"] == 0
    assert resumen["reentregas"] == 1  # el que se procesó pero no alcanzó a confirmarse


async def test_al_reiniciar_el_rezago_se_repone_desde_la_base(entorno):
    for i in range(3):
        entorno.enviar(evento(evento_id=f"e-{i}"))
    await entorno.consumidor.procesar_lote()  # procesa los 3
    entorno.enviar(evento(evento_id="e-3"))  # llega uno más y no se procesa

    # Proceso nuevo: bus vacío, misma base.
    from app.siniestros.servicio import Receptor

    nuevo_bus = BusMemoria(particiones=4)
    receptor = Receptor(entorno.repo, nuevo_bus, reloj=lambda: AHORA)

    assert await receptor.reponer_rezago() == 1
    assert await nuevo_bus.rezago(TOPIC_EVENTOS, "cualquiera") == 1


async def test_una_rafaga_concurrente_con_duplicados_no_pierde_ni_duplica(entorno_concurrente):
    entorno = entorno_concurrente
    cuerpos = [evento(evento_id=f"e-{i % 40}") for i in range(120)]  # 40 distintos, 3 veces cada uno

    await asyncio.gather(*(entorno.receptor.recibir(c, "proveedor-sismos") for c in cuerpos))
    await entorno.consumidor.drenar()

    resumen = await entorno.receptor.resumen()
    assert resumen["recibidos"] == 40 and resumen["publicados"] == 40
    assert resumen["procesados"] == 40 and resumen["rezago"] == 0 and resumen["reentregas"] == 0
