import asyncio

import pytest

from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import (
    PERFIL_POR_DEFECTO,
    EstadoCircuito,
    EstadoConsentimiento,
    Fuente,
)
from app.proveedores.simuladores import ModoFalla


async def test_consentimiento_otorgado_consulta_ambas_fuentes_con_origen(construir):
    servicio, _, _ = construir()
    perfil = await servicio.obtener_perfil("c1")
    assert {f.fuente for f in perfil.fuentes} == {Fuente.OPEN_FINANCE, Fuente.OPEN_DATA}
    assert all(f.payload and not f.degradado for f in perfil.fuentes)
    assert not perfil.por_defecto


@pytest.mark.parametrize(
    "estado", [EstadoConsentimiento.NO_OTORGADO, EstadoConsentimiento.REVOCADO]
)
async def test_sin_consentimiento_no_llama_a_proveedores(
    construir, estado, monkeypatch
):
    servicio, proveedores, _ = construir(consentimiento=ConsentimientoStub(estado))
    llamadas = []
    for p in proveedores.values():
        monkeypatch.setattr(p, "consultar_perfil", lambda *_: llamadas.append(1))
    perfil = await servicio.obtener_perfil("c1")
    assert llamadas == []
    assert perfil.por_defecto and perfil.fuentes == []


async def test_cache_aside_miss_luego_hit_sin_llamar_al_proveedor(
    construir, monkeypatch
):
    servicio, proveedores, llamadas = construir()
    await servicio.obtener_perfil("c1")  # miss: consulta y cachea

    async def no_debe_llamarse(*_):
        raise AssertionError("el proveedor no debía consultarse en un hit")

    for p in proveedores.values():
        monkeypatch.setattr(p, "consultar_perfil", no_debe_llamarse)
    perfil = await servicio.obtener_perfil("c1")
    assert all(f.hit_cache for f in perfil.fuentes)
    assert [r.hit_cache for r in llamadas[-2:]] == [True, True]


async def test_ttl_cacheado_respeta_jitter(construir, redis):
    servicio, _, _ = construir()
    await servicio.obtener_perfil("c1")
    ttl = await redis.ttl("perfil:c1:open_finance")
    assert 255 <= ttl <= 345


async def test_timeout_duro_degrada_sin_fallar(construir):
    servicio, _, _ = construir(modo_of=ModoFalla.TIMEOUT)
    perfil = await asyncio.wait_for(servicio.obtener_perfil("c1"), timeout=1)
    of = next(f for f in perfil.fuentes if f.fuente is Fuente.OPEN_FINANCE)
    assert of.degradado and of.por_defecto and of.payload == PERFIL_POR_DEFECTO
    assert perfil.degradado


@pytest.mark.parametrize("modo", [ModoFalla.ERROR_5XX, ModoFalla.VACIO])
async def test_error_5xx_y_respuesta_vacia_degradan(construir, modo):
    servicio, _, _ = construir(modo_of=modo)
    perfil = await servicio.obtener_perfil("c1")
    of = next(f for f in perfil.fuentes if f.fuente is Fuente.OPEN_FINANCE)
    assert of.degradado


async def test_degradado_sirve_ultimo_perfil_cacheado(construir, redis):
    servicio, proveedores, _ = construir()
    original = await servicio.obtener_perfil("c1")
    await redis.delete("perfil:c1:open_finance")  # expiró el vigente; queda el respaldo
    proveedores[Fuente.OPEN_FINANCE].config = type(
        proveedores[Fuente.OPEN_FINANCE].config
    )(modo=ModoFalla.ERROR_5XX)
    perfil = await servicio.obtener_perfil("c1")
    of = next(f for f in perfil.fuentes if f.fuente is Fuente.OPEN_FINANCE)
    esperado = next(f for f in original.fuentes if f.fuente is Fuente.OPEN_FINANCE)
    assert of.degradado and not of.por_defecto and of.payload == esperado.payload


async def test_circuito_abre_tras_5_fallos_y_deja_de_salir_a_la_red(
    construir, monkeypatch
):
    servicio, proveedores, _ = construir(modo_of=ModoFalla.ERROR_5XX)
    for i in range(5):
        await servicio.obtener_perfil(f"c{i}")
    assert servicio.breakers[Fuente.OPEN_FINANCE].estado is EstadoCircuito.ABIERTO

    llamadas = []

    async def espia(*_):
        llamadas.append(1)
        return {"x": 1}

    monkeypatch.setattr(proveedores[Fuente.OPEN_FINANCE], "consultar_perfil", espia)
    perfil = await servicio.obtener_perfil("nuevo")
    assert llamadas == []
    assert next(f for f in perfil.fuentes if f.fuente is Fuente.OPEN_FINANCE).degradado


async def test_sondeo_en_segundo_plano_cierra_el_circuito_sin_solicitud_de_usuario(
    construir,
):
    servicio, proveedores, _ = construir(modo_of=ModoFalla.ERROR_5XX)
    for i in range(5):
        await servicio.obtener_perfil(f"c{i}")
    proveedores[Fuente.OPEN_FINANCE].config = type(
        proveedores[Fuente.OPEN_FINANCE].config
    )()
    await servicio.sondear_una_vez()
    assert servicio.breakers[Fuente.OPEN_FINANCE].estado is EstadoCircuito.CERRADO


async def test_sondeo_fallido_mantiene_circuito_abierto(construir):
    servicio, _, _ = construir(modo_of=ModoFalla.ERROR_5XX)
    for i in range(5):
        await servicio.obtener_perfil(f"c{i}")
    await servicio.sondear_una_vez()
    assert servicio.breakers[Fuente.OPEN_FINANCE].estado is EstadoCircuito.ABIERTO


async def test_ninguna_solicitud_falla_con_proveedor_caido_en_rafaga(construir):
    servicio, _, _ = construir(modo_of=ModoFalla.ERROR_5XX, modo_od=ModoFalla.TIMEOUT)
    perfiles = await asyncio.gather(
        *(servicio.obtener_perfil(f"c{i}") for i in range(200))
    )
    assert len(perfiles) == 200 and all(p.degradado for p in perfiles)


async def test_registro_por_llamada_incluye_metricas_sin_payload(construir):
    servicio, _, llamadas = construir()
    await servicio.obtener_perfil("c1")
    assert len(llamadas) == 2
    r = llamadas[0]
    assert r.latencia_ms >= 0 and r.estado_circuito is EstadoCircuito.CERRADO
    assert not hasattr(r, "payload")
