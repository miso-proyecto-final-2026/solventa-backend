"""Gestión de cobros fallidos (HU19): casos, gracia, reintento idempotente, notas y aviso al operador."""

from datetime import timedelta

import pytest
from app.pagos.acceso import Gestor
from app.pagos.errores import (
    CasoNoEncontrado,
    CasoYaRecuperado,
    DatoDeTarjetaNoPermitido,
    SolicitudInvalida,
)
from app.pagos.gestion import GRACIA_VENCIDA, ServicioGestion
from app.pagos.modelos_db import CasoCobroFallidoDB, EventoSalidaDB, TransaccionDB
from app.pagos.servicio import ComandoCobro
from sqlalchemy import select

from tests.dobles import TOKEN, PasarelaFalsa, comando

ASESORA = Gestor("ase-1", "Asesora Demo", "ASESOR")
PAN_PRUEBA = "4111 1111 1111 1111"  # número de prueba público de Visa, no una tarjeta real


@pytest.fixture
def montar(armar, fabrica, reloj):
    """(cobros, gestión, pasarela) con la misma pasarela falsa y el mismo reloj."""

    def _montar(*guion):
        pasarela = PasarelaFalsa(*guion)
        cobros = armar(pasarela)
        return cobros, ServicioGestion(fabrica=fabrica, cobros=cobros, horas_por_vencer=24, reloj=reloj), pasarela

    return _montar


async def _rechazado(cobros, suscripcion_id="susc-1"):
    tx, _ = await cobros.cobrar(ComandoCobro.desde_evento(comando(suscripcion_id)))
    assert tx.estado == "RECHAZADA"
    return tx


def _caso(fabrica, caso_id):
    with fabrica() as s:
        return s.get(CasoCobroFallidoDB, caso_id)


def _eventos(fabrica, event_type=None):
    with fabrica() as s:
        filas = s.execute(select(EventoSalidaDB).order_by(EventoSalidaDB.id)).scalars().all()
    return [f for f in filas if event_type is None or f.payload["event_type"] == event_type]


# --- apertura del caso -----------------------------------------------------------------------------------------

async def test_un_rechazo_abre_el_caso_con_la_gracia_de_tres_dias(montar, fabrica, reloj):
    cobros, _, _ = montar("rechazar")
    tx = await _rechazado(cobros)

    caso = _caso(fabrica, tx.id)
    assert caso.estado == "PENDIENTE"
    assert caso.gracia_hasta.replace(tzinfo=reloj.ahora.tzinfo) == reloj.ahora + timedelta(days=3)
    assert (caso.codigo_rechazo, caso.motivo_rechazo, caso.reintentos) == ("FONDOS_INSUFICIENTES", "Sin fondos", 0)


async def test_un_cobro_aprobado_no_abre_caso(montar, fabrica):
    cobros, _, _ = montar("aprobar")
    tx, _ = await cobros.cobrar(ComandoCobro.desde_evento(comando()))
    assert _caso(fabrica, tx.id) is None


# --- bandeja (criterios 1 y 2) ---------------------------------------------------------------------------------

async def test_la_bandeja_ordena_por_gracia_y_marca_los_proximos_a_vencer(montar, reloj):
    cobros, gestion, _ = montar("rechazar", "rechazar", "rechazar")
    viejo = await _rechazado(cobros, "susc-vieja")
    reloj.avanzar(60 * 60 * 24 * 2 + 3600)          # a la primera le quedan 23 h
    medio = await _rechazado(cobros, "susc-media")
    reloj.avanzar(60)
    nuevo = await _rechazado(cobros, "susc-nueva")

    pagina = gestion.bandeja(estados=None, solo_por_vencer=False, pagina=1, tamano=20)
    assert [v.caso.id for v in pagina.casos] == [viejo.id, medio.id, nuevo.id]
    assert [v.estado_gracia for v in pagina.casos] == ["POR_VENCER", "VIGENTE", "VIGENTE"]
    assert pagina.casos[0].token.ultimos4 == "4242"

    solo = gestion.bandeja(estados=None, solo_por_vencer=True, pagina=1, tamano=20)
    assert [v.caso.id for v in solo.casos] == [viejo.id]

    reloj.avanzar(60 * 60 * 23)
    assert gestion.bandeja(estados=None, solo_por_vencer=False, pagina=1, tamano=20).casos[0].estado_gracia == "VENCIDA"


async def test_la_bandeja_pagina_y_cuenta_el_total(montar):
    cobros, gestion, _ = montar(*["rechazar"] * 5)
    for i in range(5):
        await _rechazado(cobros, f"susc-{i}")

    primera = gestion.bandeja(estados=None, solo_por_vencer=False, pagina=1, tamano=2)
    tercera = gestion.bandeja(estados=None, solo_por_vencer=False, pagina=3, tamano=2)
    assert (len(primera.casos), primera.total) == (2, 5)
    assert (len(tercera.casos), tercera.total) == (1, 5)


async def test_la_bandeja_por_defecto_no_muestra_los_recuperados(montar):
    cobros, gestion, _ = montar("rechazar", "aprobar")
    tx = await _rechazado(cobros)
    await gestion.reintentar(tx.id, ASESORA)

    assert gestion.bandeja(estados=None, solo_por_vencer=False, pagina=1, tamano=20).total == 0
    recuperados = gestion.bandeja(estados=["RECUPERADO"], solo_por_vencer=False, pagina=1, tamano=20)
    assert [v.estado_gracia for v in recuperados.casos] == [None]


# --- reintento (criterio 4) ------------------------------------------------------------------------------------

async def test_un_reintento_aprobado_recupera_el_cobro_con_el_mismo_token_y_una_clave_nueva(montar, fabrica):
    cobros, gestion, pasarela = montar("rechazar", "aprobar")
    tx = await _rechazado(cobros)

    r = await gestion.reintentar(tx.id, ASESORA)

    assert (r.transaccion.estado, r.numero, r.creado, r.caso.estado) == ("APROBADA", 1, True, "RECUPERADO")
    assert r.transaccion.idempotency_key == "cobro_prima:susc-1:1"
    assert pasarela.ordenes[-1].token == TOKEN
    confirmados = _eventos(fabrica, "cobro_prima_confirmado")
    assert [e.payload["payload"]["transaccion_id"] for e in confirmados] == [r.transaccion.id]
    with pytest.raises(CasoYaRecuperado):
        await gestion.reintentar(tx.id, ASESORA)


async def test_un_reintento_rechazado_no_extiende_la_gracia_y_el_siguiente_usa_otra_clave(montar, fabrica, reloj):
    cobros, gestion, pasarela = montar("rechazar", "rechazar", "aprobar")
    tx = await _rechazado(cobros)
    gracia = _caso(fabrica, tx.id).gracia_hasta
    reloj.avanzar(3600)

    primero = await gestion.reintentar(tx.id, ASESORA)
    assert (primero.transaccion.estado, primero.caso.estado, primero.caso.reintentos) == ("RECHAZADA", "PENDIENTE", 1)
    assert primero.caso.gracia_hasta == gracia
    rechazo = _eventos(fabrica, "cobro_prima_rechazado")[-1].payload["payload"]
    assert rechazo["gracia_hasta"] == tx.gracia_hasta.isoformat()

    segundo = await gestion.reintentar(tx.id, ASESORA)
    assert segundo.transaccion.idempotency_key == "cobro_prima:susc-1:2"
    assert (segundo.transaccion.estado, segundo.caso.estado) == ("APROBADA", "RECUPERADO")
    assert len(pasarela.ordenes) == 3


async def test_dos_clics_con_la_misma_clave_de_solicitud_no_cobran_dos_veces(montar):
    cobros, gestion, pasarela = montar("rechazar", "rechazar")
    tx = await _rechazado(cobros)

    primero = await gestion.reintentar(tx.id, ASESORA, "clic-1")
    segundo = await gestion.reintentar(tx.id, ASESORA, "clic-1")

    assert (primero.creado, segundo.creado) == (True, False)
    assert segundo.transaccion.id == primero.transaccion.id
    assert len(pasarela.ordenes) == 2  # el cobro original y un solo reintento


async def test_con_un_reintento_en_curso_otro_clic_devuelve_el_mismo_sin_cobrar(montar, fabrica):
    cobros, gestion, pasarela = montar("rechazar", "caida")
    tx = await _rechazado(cobros)

    en_curso = await gestion.reintentar(tx.id, ASESORA)
    assert (en_curso.transaccion.estado, en_curso.ultimo_intento) == ("PENDIENTE", "NO_DISPONIBLE")

    otro = await gestion.reintentar(tx.id, ASESORA, "clic-distinto")
    assert (otro.creado, otro.transaccion.id) == (False, en_curso.transaccion.id)
    assert len(pasarela.ordenes) == 2


async def test_el_procesador_resuelve_despues_un_reintento_que_quedo_en_curso(montar, fabrica, reloj):
    cobros, gestion, _ = montar("rechazar", "caida", "aprobar")
    tx = await _rechazado(cobros)
    await gestion.reintentar(tx.id, ASESORA)

    reloj.avanzar(60)
    await cobros.procesar_pendientes()

    assert _caso(fabrica, tx.id).estado == "RECUPERADO"


async def test_reintentar_un_caso_inexistente_es_404(montar):
    _, gestion, _ = montar()
    with pytest.raises(CasoNoEncontrado):
        await gestion.reintentar("no-existe", ASESORA)


async def test_una_clave_de_solicitud_demasiado_larga_se_rechaza(montar):
    cobros, gestion, _ = montar("rechazar")
    tx = await _rechazado(cobros)
    with pytest.raises(SolicitudInvalida):
        await gestion.reintentar(tx.id, ASESORA, "x" * 101)


# --- notas (criterio 5) ----------------------------------------------------------------------------------------

async def test_la_nota_queda_con_autor_y_fecha(montar, reloj):
    cobros, gestion, _ = montar("rechazar")
    tx = await _rechazado(cobros)

    nota = gestion.agregar_nota(tx.id, "  Cliente dice que actualiza la tarjeta mañana  ", ASESORA)

    assert (nota.autor_id, nota.autor_nombre, nota.autor_rol) == ("ase-1", "Asesora Demo", "ASESOR")
    assert nota.texto == "Cliente dice que actualiza la tarjeta mañana"
    assert nota.created_at == reloj.ahora
    assert [n.texto for n in gestion.detalle(tx.id).notas] == [nota.texto]


@pytest.mark.parametrize("texto, error", [("   ", SolicitudInvalida), ("x" * 1001, SolicitudInvalida), (f"tarjeta {PAN_PRUEBA}", DatoDeTarjetaNoPermitido)])
async def test_una_nota_vacia_larga_o_con_numero_de_tarjeta_se_rechaza(montar, texto, error):
    cobros, gestion, _ = montar("rechazar")
    tx = await _rechazado(cobros)
    with pytest.raises(error):
        gestion.agregar_nota(tx.id, texto, ASESORA)


def test_una_nota_sobre_un_caso_inexistente_es_404(montar):
    _, gestion, _ = montar()
    with pytest.raises(CasoNoEncontrado):
        gestion.agregar_nota("no-existe", "hola", ASESORA)


# --- detalle (criterio 3) --------------------------------------------------------------------------------------

async def test_el_detalle_trae_el_cobro_original_y_los_reintentos_con_sus_intentos(montar):
    cobros, gestion, _ = montar("rechazar", "rechazar")
    tx = await _rechazado(cobros)
    await gestion.reintentar(tx.id, ASESORA)

    detalle = gestion.detalle(tx.id)

    assert [t.id == tx.id for t, _ in detalle.transacciones] == [True, False]
    assert [[i.codigo for i in intentos] for _, intentos in detalle.transacciones] == [
        ["FONDOS_INSUFICIENTES"],
        ["FONDOS_INSUFICIENTES"],
    ]
    with pytest.raises(CasoNoEncontrado):
        gestion.detalle("no-existe")


# --- gracia vencida (criterio 6) -------------------------------------------------------------------------------

async def test_al_vencer_la_gracia_se_avisa_al_operador_una_sola_vez(montar, fabrica, reloj):
    cobros, gestion, _ = montar("rechazar")
    tx = await _rechazado(cobros)

    assert gestion.vencer_casos() == 0
    reloj.avanzar(60 * 60 * 24 * 3)
    assert gestion.vencer_casos() == 1
    assert gestion.vencer_casos() == 0

    caso = _caso(fabrica, tx.id)
    assert (caso.estado, caso.operador_notificado_en is not None) == ("VENCIDO", True)
    avisos = _eventos(fabrica, GRACIA_VENCIDA)
    assert len(avisos) == 1
    assert avisos[0].topic == "solventa.notificaciones"
    assert avisos[0].payload["payload"]["destinatario_rol"] == "OPERADOR"
    assert TOKEN not in str(avisos[0].payload)


async def test_un_caso_vencido_todavia_se_puede_recuperar(montar, fabrica, reloj):
    cobros, gestion, _ = montar("rechazar", "aprobar")
    tx = await _rechazado(cobros)
    reloj.avanzar(60 * 60 * 24 * 4)
    gestion.vencer_casos()

    r = await gestion.reintentar(tx.id, ASESORA)

    assert r.caso.estado == "RECUPERADO"


async def test_un_caso_recuperado_no_se_vence(montar, fabrica, reloj):
    cobros, gestion, _ = montar("rechazar", "aprobar")
    tx = await _rechazado(cobros)
    await gestion.reintentar(tx.id, ASESORA)
    reloj.avanzar(60 * 60 * 24 * 4)

    assert gestion.vencer_casos() == 0
    assert _eventos(fabrica, GRACIA_VENCIDA) == []


async def test_las_transacciones_del_reintento_registran_su_origen(montar, fabrica):
    cobros, gestion, _ = montar("rechazar", "aprobar")
    tx = await _rechazado(cobros)
    r = await gestion.reintentar(tx.id, ASESORA)
    with fabrica() as s:
        assert s.get(TransaccionDB, r.transaccion.id).origen == "gestion"
