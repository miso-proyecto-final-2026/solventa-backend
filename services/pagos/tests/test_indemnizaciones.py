"""Pago automático de la indemnización (HU29): consumo del comando, pago idempotente, fallos y trazabilidad.

Usa la pasarela falsa (el puerto), no el simulador: estas pruebas no cambian al cambiar de pasarela (EC-M2.2).
"""

import logging
from decimal import Decimal

import pytest
from app.pagos.dominio import identificador_parece_pan
from app.pagos.errores import ClaveReutilizada, SolicitudInvalida
from app.pagos.indemnizaciones import ComandoIndemnizacion, cuenta_por_defecto
from app.pagos.modelos_db import EventoSalidaDB, IndemnizacionDB, IntentoPagoDB
from sqlalchemy import select

from tests.dobles import PasarelaFalsa, comando_indemnizacion, evento_indemnizacion


async def _recibir(servicio, fabrica, ev=None):
    """Lo que hace el consumidor de Kafka: el manejador corre dentro de una transacción."""
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(ev or evento_indemnizacion(), s)


def _indemnizaciones(fabrica):
    with fabrica() as s:
        return list(s.execute(select(IndemnizacionDB)).scalars())


def _salidas(fabrica):
    with fabrica() as s:
        return [x.payload for x in s.execute(select(EventoSalidaDB).order_by(EventoSalidaDB.id)).scalars()]


def _intentos(fabrica):
    with fabrica() as s:
        return [(i.numero, i.resultado) for i in s.execute(select(IntentoPagoDB).order_by(IntentoPagoDB.id)).scalars()]


# --- criterios 1, 2, 4 y 7: consume el comando, paga y publica el resultado ---------------------------------------


async def test_un_comando_produce_un_pago_y_publica_indemnizacion_pagada(armar_indemnizaciones, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    await _recibir(servicio, fabrica)

    (ind,) = _indemnizaciones(fabrica)
    assert (ind.estado, Decimal(ind.monto), ind.moneda) == ("PENDIENTE", Decimal("5000000.00"), "COP")

    assert await servicio.procesar_pendientes() == 1  # sin ninguna acción manual (criterio 7)
    (ind,) = _indemnizaciones(fabrica)
    assert ind.estado == "PAGADA" and ind.referencia_pasarela == "pg-1"
    assert [(o.monto, o.moneda, o.referencia) for o in pasarela.ordenes_pago] == [(Decimal("5000000.00"), "COP", "evt-1")]

    (salida,) = _salidas(fabrica)
    assert salida["event_type"] == "indemnizacion_pagada" and salida["source"] == "ms-pagos"
    carga = salida["payload"]
    assert {k: carga[k] for k in ("evento_id", "poliza_id", "estado", "monto", "moneda", "referencia_pasarela", "idempotency_key", "repeticion")} == {
        "evento_id": "evt-1",
        "poliza_id": "pol-1",
        "estado": "PAGADA",
        "monto": "5000000.00",
        "moneda": "COP",
        "referencia_pasarela": "pg-1",
        "idempotency_key": "evt-1:pol-1",
        "repeticion": False,
    }


async def test_el_resultado_sale_por_el_topic_con_la_clave_evento_id(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones()
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()
    with fabrica() as s:
        (salida,) = s.execute(select(EventoSalidaDB)).scalars()
    assert (salida.topic, salida.clave) == ("solventa.pagos.indemnizaciones", "evt-1")


async def test_el_resultado_conserva_correlacion_y_traza_del_comando(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones()
    ev = evento_indemnizacion()
    await _recibir(servicio, fabrica, ev)
    await servicio.procesar_pendientes()
    (salida,) = _salidas(fabrica)
    assert salida["correlation_id"] == "corr-ind-1" and salida["trace_id"] == ev.trace_id


async def test_un_evento_de_otro_tipo_se_ignora(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones()
    await _recibir(servicio, fabrica, evento_indemnizacion(event_type="otra_cosa"))
    assert _indemnizaciones(fabrica) == []


# --- criterios 3 y 8: un solo pago ----------------------------------------------------------------------------------


async def test_dos_entregas_del_mismo_comando_producen_un_solo_pago(armar_indemnizaciones, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()
    await _recibir(servicio, fabrica, evento_indemnizacion())  # reentrega con otro event_id de sobre

    assert len(_indemnizaciones(fabrica)) == 1
    assert pasarela.cargos == 1 and len(pasarela.ordenes_pago) == 1
    (ind,) = _indemnizaciones(fabrica)
    assert ind.reentregas == 1
    pagadas = [s for s in _salidas(fabrica) if s["event_type"] == "indemnizacion_pagada"]
    assert [p["payload"]["repeticion"] for p in pagadas] == [False, True]  # la segunda respuesta es la de la primera
    assert pagadas[0]["payload"]["referencia_pasarela"] == pagadas[1]["payload"]["referencia_pasarela"]


async def test_la_misma_clave_cien_veces_produce_un_pago(armar_indemnizaciones, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    for _ in range(100):
        await _recibir(servicio, fabrica)
        await servicio.procesar_pendientes()
    assert pasarela.cargos == 1 and len(_indemnizaciones(fabrica)) == 1


async def test_una_repeticion_mientras_el_pago_sigue_pendiente_no_anuncia_nada(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones()
    await _recibir(servicio, fabrica)
    await _recibir(servicio, fabrica)
    assert _salidas(fabrica) == []
    assert _indemnizaciones(fabrica)[0].reentregas == 1


async def test_procesar_dos_veces_la_misma_indemnizacion_no_hace_un_segundo_intento(armar_indemnizaciones, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    await _recibir(servicio, fabrica)
    (ind,) = _indemnizaciones(fabrica)
    await servicio.procesar(ind.id)
    assert await servicio.procesar(ind.id) is None
    assert len(pasarela.ordenes_pago) == 1


async def test_la_clave_con_otro_monto_es_un_conflicto(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones()
    await _recibir(servicio, fabrica)
    with pytest.raises(ClaveReutilizada):
        await _recibir(servicio, fabrica, evento_indemnizacion(comando_indemnizacion(monto_indemnizacion=1)))


async def test_otra_clave_para_el_mismo_par_evento_poliza_no_crea_un_segundo_pago(armar_indemnizaciones, fabrica):
    pasarela = PasarelaFalsa()
    servicio = armar_indemnizaciones(pasarela)
    await _recibir(servicio, fabrica)
    await _recibir(servicio, fabrica, evento_indemnizacion(comando_indemnizacion(idempotency_key="otra-clave")))
    await servicio.procesar_pendientes()
    assert len(_indemnizaciones(fabrica)) == 1 and pasarela.cargos == 1


# --- criterio 6: fallos de la pasarela ---------------------------------------------------------------------------


async def test_un_rechazo_deja_la_indemnizacion_fallida_y_publica_indemnizacion_fallida(armar_indemnizaciones, fabrica):
    servicio = armar_indemnizaciones(PasarelaFalsa("rechazar"))
    await _recibir(servicio, fabrica)
    await servicio.procesar_pendientes()
    (ind,) = _indemnizaciones(fabrica)
    assert (ind.estado, ind.codigo_fallo) == ("FALLIDA", "CUENTA_CERRADA")
    (salida,) = _salidas(fabrica)
    assert salida["event_type"] == "indemnizacion_fallida"
    assert (salida["payload"]["codigo"], salida["payload"]["intentos"]) == ("CUENTA_CERRADA", 1)


async def test_si_la_pasarela_esta_caida_reintenta_con_retroceso_exponencial_y_se_recupera(
    armar_indemnizaciones, fabrica, reloj
):
    pasarela = PasarelaFalsa("caida", "caida", "aprobar")
    servicio = armar_indemnizaciones(pasarela, reintento_base_s=5)
    await _recibir(servicio, fabrica)
    (ind,) = _indemnizaciones(fabrica)

    await servicio.procesar(ind.id)
    assert _indemnizaciones(fabrica)[0].estado == "PENDIENTE"
    assert await servicio.procesar(ind.id) is None  # aún no le toca (espera de 5 s)
    reloj.avanzar(5)
    await servicio.procesar(ind.id)  # segundo intento: espera de 10 s
    reloj.avanzar(9)
    assert await servicio.procesar(ind.id) is None
    reloj.avanzar(1)
    await servicio.procesar(ind.id)

    assert _indemnizaciones(fabrica)[0].estado == "PAGADA"
    # La pasarela no recibió las órdenes caídas: al consultar no hay cobro previo y se paga.
    assert [r for _, r in _intentos(fabrica)] == ["NO_DISPONIBLE", "NO_DISPONIBLE", "APROBADO"]


async def test_si_la_respuesta_se_pierde_se_concilia_sin_volver_a_pagar(armar_indemnizaciones, fabrica, reloj):
    pasarela = PasarelaFalsa("perder")
    servicio = armar_indemnizaciones(pasarela)
    await _recibir(servicio, fabrica)
    (ind,) = _indemnizaciones(fabrica)
    await servicio.procesar(ind.id)
    assert _indemnizaciones(fabrica)[0].estado == "PENDIENTE"
    reloj.avanzar(5)
    await servicio.procesar(ind.id)

    assert _indemnizaciones(fabrica)[0].estado == "PAGADA"
    assert pasarela.cargos == 1 and len(pasarela.ordenes_pago) == 1  # el segundo intento solo consultó
    assert [r for _, r in _intentos(fabrica)] == ["SIN_RESPUESTA", "CONCILIADO"]


async def test_agotados_los_intentos_queda_fallida_y_visible_sin_perderse(armar_indemnizaciones, fabrica, reloj):
    servicio = armar_indemnizaciones(PasarelaFalsa("caida", "caida", "caida"), max_intentos=3)
    await _recibir(servicio, fabrica)
    (ind,) = _indemnizaciones(fabrica)
    for _ in range(3):
        await servicio.procesar(ind.id)
        reloj.avanzar(60)

    (ind,) = _indemnizaciones(fabrica)
    assert (ind.estado, ind.codigo_fallo, ind.intentos) == ("FALLIDA", "INTENTOS_AGOTADOS", 3)
    (salida,) = _salidas(fabrica)
    assert salida["event_type"] == "indemnizacion_fallida" and salida["payload"]["codigo"] == "INTENTOS_AGOTADOS"


def test_los_intentos_maximos_deben_ser_positivos(fabrica):
    from app.pagos.indemnizaciones import ServicioIndemnizaciones

    with pytest.raises(ValueError):
        ServicioIndemnizaciones(fabrica=fabrica, pasarela=PasarelaFalsa(), max_intentos=0)


# --- criterio 9: trazabilidad ----------------------------------------------------------------------------------------


async def test_cada_pago_conserva_origen_monto_referencia_y_los_estados_por_los_que_paso(armar_indemnizaciones, fabrica, reloj):
    servicio = armar_indemnizaciones(PasarelaFalsa("perder"))
    await _recibir(servicio, fabrica)
    (ind,) = _indemnizaciones(fabrica)
    await servicio.procesar(ind.id)
    reloj.avanzar(5)
    await servicio.procesar(ind.id)

    consultado, intentos = servicio.consultar(ind.id)
    assert (consultado.evento_id, consultado.poliza_id, Decimal(consultado.monto), consultado.estado) == (
        "evt-1", "pol-1", Decimal("5000000.00"), "PAGADA",
    )
    assert consultado.referencia_pasarela is not None and consultado.regla == "sismo" and consultado.version_reglas
    assert [(i.numero, i.resultado) for i in intentos] == [(1, "SIN_RESPUESTA"), (2, "CONCILIADO")]
    assert servicio.consultar("no-existe") is None


# --- criterio 5: ninguna cuenta en claro -------------------------------------------------------------------------


async def test_ni_el_evento_ni_los_logs_llevan_la_cuenta_de_destino(armar_indemnizaciones, fabrica, caplog):
    servicio = armar_indemnizaciones(PasarelaFalsa("perder"))
    cuenta = "cta_secreta_de_prueba"
    with caplog.at_level(logging.DEBUG):
        await _recibir(servicio, fabrica, evento_indemnizacion(comando_indemnizacion(cuenta_destino=cuenta)))
        (ind,) = _indemnizaciones(fabrica)
        await servicio.procesar(ind.id)
    assert cuenta not in caplog.text
    assert all(cuenta not in str(s) for s in _salidas(fabrica))
    assert servicio.consultar(ind.id)[0].cuenta_destino == cuenta  # solo vive en la base, como token


def test_la_cuenta_de_destino_con_forma_de_tarjeta_se_rechaza_sin_copiarla():
    pan = "4111111111111111"
    with pytest.raises(SolicitudInvalida) as error:
        ComandoIndemnizacion.desde_evento(comando_indemnizacion(cuenta_destino=pan))
    assert pan not in str(error.value.detalles) and error.value.detalles[0]["campo"] == "cuenta_destino"


def test_sin_cuenta_en_el_comando_se_usa_una_cuenta_simulada_opaca_por_poliza():
    comando = ComandoIndemnizacion.desde_evento(comando_indemnizacion())
    assert comando.cuenta_destino == cuenta_por_defecto("pol-1") and comando.cuenta_destino.startswith("cta_sim_")
    assert not identificador_parece_pan(comando.cuenta_destino) and "pol-1" not in comando.cuenta_destino


# --- validación del comando -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cambio",
    [{"monto_indemnizacion": None}, {"monto_indemnizacion": 0}, {"monto_indemnizacion": "abc"}, {"moneda": "USD"},
     {"evento_id": None}, {"poliza_id": None}, {"idempotency_key": "x" * 101}],
)
def test_un_comando_invalido_se_rechaza(cambio):
    with pytest.raises(SolicitudInvalida):
        ComandoIndemnizacion.desde_evento(comando_indemnizacion(**cambio))


def test_sin_idempotency_key_se_deriva_del_par_evento_poliza():
    comando = ComandoIndemnizacion.desde_evento(comando_indemnizacion(idempotency_key=None))
    assert comando.idempotency_key == "evt-1:pol-1"
