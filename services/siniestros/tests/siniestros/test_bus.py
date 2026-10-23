"""El bus en memoria: orden por partición, confirmación, contrapresión (criterio 6)."""

import pytest

from app.siniestros.bus import BusMemoria, BusSaturado, particion_de

T = "solventa.siniestros.eventos"


async def test_los_mensajes_de_una_clave_quedan_en_orden_en_la_misma_particion():
    bus = BusMemoria(particiones=4)
    for i in range(5):
        await bus.publicar(T, "pol-1", {"n": i})
    await bus.publicar(T, "pol-2", {"n": 99})

    leidos = [m for m in await bus.leer(T, "g", 100) if m.clave == "pol-1"]

    assert [m.valor["n"] for m in leidos] == [0, 1, 2, 3, 4]
    assert len({m.particion for m in leidos}) == 1


def test_la_particion_es_estable_entre_llamadas():
    assert {particion_de("pol-1", 6) for _ in range(10)} == {particion_de("pol-1", 6)}


async def test_lo_no_confirmado_se_vuelve_a_leer_y_lo_confirmado_no():
    bus = BusMemoria(particiones=1)
    for i in range(3):
        await bus.publicar(T, "k", {"n": i})
    primero, *_ = await bus.leer(T, "g", 10)

    await bus.confirmar("g", primero)

    assert [m.valor["n"] for m in await bus.leer(T, "g", 10)] == [1, 2]
    assert await bus.rezago(T, "g") == 2


async def test_cada_grupo_lleva_su_propio_avance():
    bus = BusMemoria(particiones=1)
    m = await bus.publicar(T, "k", {})
    await bus.confirmar("a", m)

    assert await bus.leer(T, "a", 10) == []
    assert len(await bus.leer(T, "b", 10)) == 1


async def test_con_el_rezago_lleno_rechaza_publicar_y_al_drenar_vuelve_a_aceptar():
    bus = BusMemoria(particiones=1, capacidad_rezago=2)
    bus.registrar_grupo(T, "g")
    await bus.publicar(T, "k", {})
    await bus.publicar(T, "k", {})

    with pytest.raises(BusSaturado):
        await bus.publicar(T, "k", {})

    for m in await bus.leer(T, "g", 10):
        await bus.confirmar("g", m)
    await bus.publicar(T, "k", {})  # no lanza


async def test_sin_grupos_registrados_no_hay_contrapresion():
    bus = BusMemoria(particiones=1, capacidad_rezago=1)

    for _ in range(3):
        await bus.publicar(T, "k", {})
