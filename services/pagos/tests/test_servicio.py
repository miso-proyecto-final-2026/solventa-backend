"""Caso de uso del cobro (criterios 1 a 4): aprobación, rechazo, idempotencia y conciliación.

Usa la pasarela falsa (el puerto), no el simulador: estas pruebas no cambian al cambiar de pasarela (EC-M2.2).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from app.pagos import repositorio as repo
from app.pagos.errores import ClaveReutilizada, SolicitudInvalida
from app.pagos.modelos_db import EventoSalidaDB, IntentoCobroDB, TransaccionDB
from app.pagos.servicio import ComandoCobro
from sqlalchemy import select

from tests.dobles import AHORA, MONTO, TOKEN, PasarelaFalsa, comando, evento


async def _recibir(servicio, fabrica, ev=None):
    """Lo que hace el consumidor de Kafka: el manejador corre dentro de una transacción."""
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(ev or evento(), s)


def _transacciones(fabrica):
    with fabrica() as s:
        return list(s.execute(select(TransaccionDB)).scalars())


def _salidas(fabrica):
    with fabrica() as s:
        return [x.payload for x in s.execute(select(EventoSalidaDB).order_by(EventoSalidaDB.id)).scalars()]


def _intentos(fabrica):
    with fabrica() as s:
        return [(i.numero, i.resultado) for i in s.execute(select(IntentoCobroDB).order_by(IntentoCobroDB.id)).scalars()]


# --- criterio 1: cobro exitoso ---------------------------------------------------------------------------------

async def test_un_cobro_aprobado_queda_registrado_con_la_referencia_y_publica_confirmado(armar, fabrica):
    pasarela = PasarelaFalsa("aprobar")
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)

    assert await servicio.procesar_pendientes() == 1

    (tx,) = _transacciones(fabrica)
    assert (tx.tipo, tx.estado, tx.referencia_pasarela) == ("COBRO_PRIMA", "APROBADA", "ref-1")
    assert (Decimal(tx.monto), tx.moneda) == (MONTO, "COP")
    assert _intentos(fabrica) == [(1, "APROBADO")]
    (sobre,) = _salidas(fabrica)
    assert sobre["event_type"] == "cobro_prima_confirmado" and sobre["source"] == "ms-pagos"
    assert sobre["payload"]["suscripcion_id"] == "susc-1" and sobre["payload"]["referencia_pasarela"] == "ref-1"
    assert sobre["payload"]["monto"] == "175000.00" and sobre["payload"]["ultimos4"] == "4242"
    assert pasarela.ordenes[0].idempotency_key == "cobro_prima:susc-1"  # la pasarela recibe la misma clave


async def test_el_resultado_conserva_la_correlacion_y_la_traza_del_comando(armar, fabrica):
    servicio = armar()
    ev = evento()
    await _recibir(servicio, fabrica, ev)
    await servicio.procesar_pendientes()

    (sobre,) = _salidas(fabrica)
    assert (sobre["correlation_id"], sobre["trace_id"]) == (ev.correlation_id, ev.trace_id)


# --- criterio 2: cobro rechazado -------------------------------------------------------------------------------

async def test_un_rechazo_registra_el_intento_con_codigo_y_motivo_y_abre_el_periodo_de_gracia(armar, fabrica):
    servicio = armar(PasarelaFalsa("rechazar"), periodo_gracia_dias=3)
    await _recibir(servicio, fabrica)

    await servicio.procesar_pendientes()

    (tx,) = _transacciones(fabrica)
    assert (tx.estado, tx.codigo_rechazo, tx.motivo_rechazo) == ("RECHAZADA", "FONDOS_INSUFICIENTES", "Sin fondos")
    assert repo.utc(tx.gracia_hasta) == AHORA + timedelta(days=3)
    with fabrica() as s:
        (intento,) = s.execute(select(IntentoCobroDB)).scalars()
    assert (intento.resultado, intento.codigo, intento.motivo) == ("RECHAZADO", "FONDOS_INSUFICIENTES", "Sin fondos")
    (sobre,) = _salidas(fabrica)
    assert sobre["event_type"] == "cobro_prima_rechazado"
    assert sobre["payload"]["codigo"] == "FONDOS_INSUFICIENTES"
    assert sobre["payload"]["gracia_hasta"].startswith("2026-10-28T12:00:00")


# --- criterio 3: idempotencia ----------------------------------------------------------------------------------

async def test_el_mismo_comando_dos_veces_cobra_una_sola_vez_y_repite_el_resultado(armar, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()

    await _recibir(servicio, fabrica, evento())  # otro event_id, misma idempotency_key (reintento de la saga)
    await servicio.procesar_pendientes()

    (tx,) = _transacciones(fabrica)
    assert tx.reentregas == 1 and pasarela.cargos == 1 and len(pasarela.ordenes) == 1
    primero, segundo = _salidas(fabrica)
    assert segundo["event_type"] == primero["event_type"] == "cobro_prima_confirmado"
    assert segundo["payload"]["transaccion_id"] == primero["payload"]["transaccion_id"]
    assert segundo["payload"]["referencia_pasarela"] == primero["payload"]["referencia_pasarela"]
    assert segundo["payload"]["repeticion"] is True


async def test_una_repeticion_mientras_el_cobro_sigue_pendiente_no_anuncia_nada(armar, fabrica):
    servicio = armar()
    await _recibir(servicio, fabrica)
    await _recibir(servicio, fabrica)

    assert len(_transacciones(fabrica)) == 1 and _salidas(fabrica) == []


async def test_la_clave_con_otro_monto_es_un_conflicto(armar, fabrica):
    servicio = armar()
    await _recibir(servicio, fabrica)

    with pytest.raises(ClaveReutilizada):
        await _recibir(servicio, fabrica, evento(comando(monto="1.00")))


async def test_procesar_dos_veces_la_misma_transaccion_no_hace_un_segundo_intento(armar, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)
    (tx,) = _transacciones(fabrica)

    await servicio.procesar(tx.id)
    assert await servicio.procesar(tx.id) is None
    assert len(pasarela.ordenes) == 1


# --- criterio 4: respuesta perdida y conciliación ----------------------------------------------------------------

async def test_si_la_respuesta_se_pierde_se_concilia_sin_volver_a_cobrar(armar, fabrica, reloj):
    pasarela = PasarelaFalsa("perder")
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)

    await servicio.procesar_pendientes()
    (tx,) = _transacciones(fabrica)
    assert tx.estado == "PENDIENTE" and _salidas(fabrica) == []

    assert await servicio.procesar_pendientes() == 0  # todavía no le toca: espera antes de conciliar
    reloj.avanzar(5)
    assert await servicio.procesar_pendientes() == 1

    (tx,) = _transacciones(fabrica)
    assert tx.estado == "APROBADA" and tx.referencia_pasarela == "ref-1"
    assert _intentos(fabrica) == [(1, "SIN_RESPUESTA"), (2, "CONCILIADO")]
    assert pasarela.cargos == 1 and len(pasarela.ordenes) == 1 and pasarela.consultas == ["cobro_prima:susc-1"]
    assert [s["event_type"] for s in _salidas(fabrica)] == ["cobro_prima_confirmado"]


async def test_si_la_pasarela_tarda_mas_que_el_tiempo_de_espera_se_concilia(armar, fabrica, reloj):
    pasarela = PasarelaFalsa("colgar")
    servicio = armar(pasarela, timeout_pasarela_s=0.05)
    await _recibir(servicio, fabrica)

    await servicio.procesar_pendientes()
    reloj.avanzar(5)
    await servicio.procesar_pendientes()

    (tx,) = _transacciones(fabrica)
    assert tx.estado == "APROBADA" and pasarela.cargos == 1
    assert _intentos(fabrica) == [(1, "SIN_RESPUESTA"), (2, "CONCILIADO")]


async def test_si_la_pasarela_no_tiene_el_cobro_se_vuelve_a_pedir_con_la_misma_clave(armar, fabrica, reloj):
    pasarela = PasarelaFalsa("caida", "aprobar")
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)

    await servicio.procesar_pendientes()
    reloj.avanzar(5)
    await servicio.procesar_pendientes()

    (tx,) = _transacciones(fabrica)
    assert tx.estado == "APROBADA"
    assert _intentos(fabrica) == [(1, "NO_DISPONIBLE"), (2, "APROBADO")]
    assert {o.idempotency_key for o in pasarela.ordenes} == {"cobro_prima:susc-1"} and pasarela.cargos == 1


async def test_si_el_proceso_cae_a_mitad_de_un_cobro_el_siguiente_intento_concilia(armar, fabrica, reloj):
    pasarela = PasarelaFalsa()
    servicio = armar(pasarela)
    await _recibir(servicio, fabrica)
    (tx,) = _transacciones(fabrica)
    # La réplica tomó la transacción y cobró, pero cayó antes de anotar el resultado.
    _, solicitud, _ = servicio._tomar(tx.id)
    await pasarela.cobrar(solicitud)

    assert await servicio.procesar_pendientes() == 0  # sigue prestada a la réplica caída
    reloj.avanzar(60)
    assert await servicio.procesar_pendientes() == 1

    assert _intentos(fabrica) == [(1, "EN_CURSO"), (2, "CONCILIADO")]
    assert pasarela.cargos == 1 and len(pasarela.ordenes) == 1


async def test_los_reintentos_sin_respuesta_esperan_cada_vez_mas(armar, fabrica, reloj):
    servicio = armar(PasarelaFalsa("caida", "caida", "caida"), reintento_base_s=5)
    await _recibir(servicio, fabrica)
    esperas = []
    for _ in range(3):
        await servicio.procesar_pendientes()
        (tx,) = _transacciones(fabrica)
        espera = (repo.utc(tx.proximo_intento_en) - reloj()).total_seconds()
        esperas.append(espera)
        reloj.avanzar(espera)
    assert esperas == [5, 10, 20]


# --- comando ---------------------------------------------------------------------------------------------------

def test_un_comando_de_hu15_sin_monto_cobra_la_prima_anual():
    c = ComandoCobro.desde_evento(comando(monto=None, periodicidad_pago=None, idempotency_key=None))
    assert (c.monto, c.periodicidad_pago, c.idempotency_key) == (MONTO, "ANUAL", "cobro_prima:susc-1")


def test_un_comando_mensual_sin_monto_cobra_la_prima_mensual():
    c = ComandoCobro.desde_evento(comando(monto=None, periodicidad_pago="MENSUAL"))
    assert c.monto == Decimal("14583.00")


@pytest.mark.parametrize(
    "cambios, campo",
    [
        ({"metodo_pago": None}, "metodo_pago"),
        ({"monto": "-5"}, "monto"),
        ({"monto": "abc"}, "monto"),
        ({"moneda": "USD"}, "moneda"),
        ({"periodicidad_pago": "SEMANAL"}, "periodicidad_pago"),
    ],
)
def test_un_comando_invalido_se_rechaza_con_el_campo(cambios, campo):
    with pytest.raises(SolicitudInvalida) as error:
        ComandoCobro.desde_evento(comando(**cambios))
    assert campo in {d["campo"] for d in error.value.detalles}


async def test_otros_eventos_del_topic_se_ignoran(armar, fabrica):
    await _recibir(armar(), fabrica, evento(event_type="compensacion_solicitada"))
    assert _transacciones(fabrica) == []


async def test_el_token_se_guarda_solo_en_tokens_tarjeta(armar, fabrica):
    servicio = armar()
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()

    with fabrica() as s:
        (tx,) = s.execute(select(TransaccionDB)).scalars()
        token = repo.token_de(s, tx)
    assert (token.token, token.ultimos4, token.marca) == (TOKEN, "4242", "VISA")
    assert TOKEN not in str(_salidas(fabrica))


async def test_si_otra_replica_gana_la_carrera_se_trata_como_repeticion(armar, fabrica, monkeypatch):
    """La réplica no vio la transacción al buscarla, pero otra la confirmó antes de insertar: decide la restricción única."""
    servicio = armar()
    await _recibir(servicio, fabrica)
    buscar = repo.por_clave
    vistas = []

    def por_clave_tardio(sesion, clave):
        vistas.append(clave)
        return None if len(vistas) == 1 else buscar(sesion, clave)

    monkeypatch.setattr(repo, "por_clave", por_clave_tardio)
    await _recibir(servicio, fabrica)

    (tx,) = _transacciones(fabrica)
    assert tx.reentregas == 1 and len(vistas) == 2
