"""Consumidor de la plantilla (HS2) conectado a Siniestros: idempotencia, fallidos y recuperación (criterio 7).

Los reintentos, la confirmación del offset y el comportamiento contra un Kafka real los prueba la plantilla
y `test_kafka.py`; aquí se verifica lo que Siniestros agrega: el manejador, el grupo y el resumen.
"""

import asyncio

from app.eventos import Evento

from tests.siniestros.conftest import evento, mensaje_de


async def test_un_evento_publicado_y_consumido_cuenta_como_procesado(entorno):
    entorno.enviar()

    await entorno.entregar_todo(entorno.consumidor())

    assert entorno.resumen()["procesados"] == 1 and entorno.resumen()["reentregas"] == 0


async def test_si_el_consumidor_cae_los_eventos_siguen_aceptados_y_al_volver_se_procesan_todos(entorno):
    # Sin consumidor, 50 eventos: el acuse sigue siendo inmediato y ninguno se pierde.
    for i in range(50):
        assert entorno.enviar(evento(evento_id=f"e-{i}")).status_code == 202
    assert entorno.resumen()["publicados"] == 50 and entorno.resumen()["procesados"] == 0

    await entorno.entregar_todo(entorno.consumidor())  # el consumidor se recupera y drena

    resumen = entorno.resumen()
    assert resumen["procesados"] == 50 and resumen["reentregas"] == 0


async def test_una_reentrega_no_duplica_el_procesamiento_y_queda_registrada(entorno):
    entorno.enviar()
    consumidor = entorno.consumidor()
    topic, clave, publicado = entorno.publicador.publicados[0]

    await consumidor.procesar(mensaje_de(topic, clave, publicado))
    await consumidor.procesar(mensaje_de(topic, clave, publicado, offset=1))  # Kafka reentrega

    resumen = entorno.resumen()
    assert resumen["procesados"] == 1 and resumen["reentregas"] == 1


async def test_un_manejador_que_falla_agota_reintentos_deshace_la_reserva_y_va_a_la_cola_de_fallidos(entorno):
    entorno.enviar()
    intentos = []

    async def falla(_evento: Evento, _sesion) -> None:
        intentos.append(1)
        raise RuntimeError("no se pudo evaluar")

    await entorno.entregar_todo(entorno.consumidor(falla))

    assert len(intentos) == 3
    assert entorno.resumen()["procesados"] == 0  # la reserva se deshizo: un reintento posterior sí procesa
    [(topic, _clave, _valor, cabeceras)] = entorno.publicador.crudos
    assert topic == "solventa.siniestros.eventos.dlq" and cabeceras["intentos"] == "3"


async def test_una_rafaga_concurrente_con_duplicados_no_pierde_ni_duplica(entorno_concurrente):
    entorno = entorno_concurrente
    cuerpos = [evento(evento_id=f"e-{i % 40}") for i in range(120)]  # 40 distintos, 3 veces cada uno

    await asyncio.gather(*(entorno.receptor.recibir(c, "proveedor-sismos") for c in cuerpos))
    await entorno.entregar_todo(entorno.consumidor())

    resumen = await entorno.receptor.resumen()
    assert len(entorno.publicador.publicados) == 40
    assert resumen["recibidos"] == 40 and resumen["publicados"] == 40
    assert resumen["procesados"] == 40 and resumen["reentregas"] == 0
