"""La saga de suscripción: iniciar, decidir y dejar los eventos en la cola de salida."""

from dataclasses import replace
from datetime import timedelta

import pytest
from app.suscripcion import repositorio as repo
from app.suscripcion.dominio import PerfilRiesgo
from app.suscripcion.errores import (
    ClaveReutilizada,
    CotizacionNoEncontrada,
    CotizacionVencida,
    CotizacionYaUsada,
    NoAutenticado,
    SolicitudInvalida,
    SuscripcionNoEncontrada,
)

from tests.dobles import (
    AHORA,
    PERFIL_ESTANDAR,
    CotizacionesFalsas,
    IdentidadFalsa,
    PerfilesFalsos,
    cotizacion,
)

PEDIDO = {"token": "token-1", "clave": "k-1", "cotizacion_id": "cot-1", "metodo_pago": "tok_visa_123", "acepta_terminos": True}


async def _iniciar(servicio, **cambios):
    return await servicio.iniciar(**{**PEDIDO, **cambios})


def _salidas(fabrica):
    with fabrica() as s:
        return repo.salidas_pendientes(s, limite=50)


# --- iniciar (criterios 1, 6 y 7) ---------------------------------------------------------------------------

async def test_iniciar_crea_la_saga_en_la_decision(armar):
    susc, creada = await _iniciar(armar())
    assert creada
    assert (susc.estado_saga, susc.paso_actual, susc.decision) == ("EN_PROGRESO", "DECISION", None)
    assert (susc.cliente_id, susc.cotizacion_id, susc.monto_asegurado) == ("cli-1", "cot-1", 40_000_000)


async def test_sin_token_o_con_token_invalido_no_autentica(armar):
    with pytest.raises(NoAutenticado):
        await _iniciar(armar(), token=None)
    with pytest.raises(NoAutenticado):
        await _iniciar(armar(), token="otro")


async def test_exige_clave_de_idempotencia_y_terminos_aceptados(armar):
    with pytest.raises(SolicitudInvalida) as sin_clave:
        await _iniciar(armar(), clave=None)
    assert sin_clave.value.detalles[0]["campo"] == "Idempotency-Key"
    with pytest.raises(SolicitudInvalida) as sin_terminos:
        await _iniciar(armar(), acepta_terminos=False)
    assert sin_terminos.value.detalles[0]["campo"] == "acepta_terminos"


async def test_cotizacion_inexistente_o_de_otro_cliente_es_no_encontrada(armar):
    with pytest.raises(CotizacionNoEncontrada):
        await _iniciar(armar(), cotizacion_id="no-existe")
    ajena = CotizacionesFalsas(cotizacion(cliente="cli-2"))
    with pytest.raises(CotizacionNoEncontrada):
        await _iniciar(armar(cotizaciones=ajena))


async def test_cotizacion_vencida_se_rechaza_con_error_propio(armar):
    vencida = CotizacionesFalsas(cotizacion(vigente_hasta=AHORA - timedelta(seconds=1)))
    with pytest.raises(CotizacionVencida):
        await _iniciar(armar(cotizaciones=vencida))


async def test_cotizacion_ya_usada_se_rechaza_con_error_propio(armar):
    servicio = armar()
    await _iniciar(servicio)
    with pytest.raises(CotizacionYaUsada):
        await _iniciar(servicio, clave="k-2")


async def test_una_cotizacion_rechazada_se_puede_volver_a_intentar(armar):
    servicio = armar(perfiles=PerfilesFalsos(replace(PERFIL_ESTANDAR, campos={"ratio_endeudamiento": 0.9, "reportes_negativos": 0})))
    primera, _ = await _iniciar(servicio)
    await servicio.decidir(primera.id)
    segunda, creada = await _iniciar(servicio, clave="k-2")
    assert creada and segunda.id != primera.id


async def test_repetir_la_clave_devuelve_la_misma_saga_sin_crear_otra(armar):
    servicio = armar()
    cotizaciones = CotizacionesFalsas()
    servicio = armar(cotizaciones=cotizaciones)
    primera, creada1 = await _iniciar(servicio)
    segunda, creada2 = await _iniciar(servicio)
    assert (creada1, creada2) == (True, False)
    assert primera.id == segunda.id
    assert cotizaciones.llamadas == 1  # la repetición ni siquiera consulta


async def test_la_clave_con_otra_cotizacion_es_un_conflicto(armar):
    servicio = armar(cotizaciones=CotizacionesFalsas(cotizacion(), cotizacion(id="cot-2")))
    await _iniciar(servicio)
    with pytest.raises(ClaveReutilizada):
        await _iniciar(servicio, cotizacion_id="cot-2")


async def test_dos_replicas_con_la_misma_clave_obtienen_la_misma_saga(armar, fabrica):
    a, b = armar(), armar()  # dos instancias del servicio sobre la misma base
    s1, _ = await _iniciar(a)
    s2, creada = await _iniciar(b)
    assert s1.id == s2.id and not creada


# --- consultar (criterio 9) -----------------------------------------------------------------------------------

async def test_consultar_solo_devuelve_la_saga_del_propio_cliente(armar):
    servicio = armar(identidad=IdentidadFalsa({"token-1": "cli-1", "token-2": "cli-2"}))
    susc, _ = await _iniciar(servicio)
    assert (await servicio.consultar(susc.id, "token-1")).id == susc.id
    with pytest.raises(SuscripcionNoEncontrada):
        await servicio.consultar(susc.id, "token-2")
    with pytest.raises(NoAutenticado):
        await servicio.consultar(susc.id, None)


# --- decidir (criterios 2 a 5 y 10) ---------------------------------------------------------------------------

async def test_aprobada_avanza_al_cobro_y_encola_el_evento_sin_guardar_el_token(armar, fabrica):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    decidida = await servicio.decidir(susc.id)

    assert (decidida.estado_saga, decidida.paso_actual, decidida.decision) == ("APROBADA", "COBRO", "APROBADA")
    assert decidida.metodo_pago_token is None
    assert decidida.reglas_version and decidida.duracion_decision_ms is not None
    (salida,) = _salidas(fabrica)
    assert (salida.topic, salida.clave) == ("solventa.suscripcion.comandos", susc.id)
    assert salida.payload["event_type"] == "cobro_prima_solicitado"
    assert salida.payload["payload"]["metodo_pago"] == "tok_visa_123"
    assert salida.payload["payload"]["cotizacion_id"] == "cot-1"


async def test_rechazada_termina_sin_cobrar_y_con_motivos_legibles(armar, fabrica):
    perfil = replace(PERFIL_ESTANDAR, campos={"ratio_endeudamiento": 0.9, "reportes_negativos": 0})
    servicio = armar(perfiles=PerfilesFalsos(perfil))
    susc, _ = await _iniciar(servicio)
    decidida = await servicio.decidir(susc.id)

    assert (decidida.estado_saga, decidida.decision) == ("RECHAZADA", "RECHAZADA")
    assert decidida.motivos and all(isinstance(m, str) and len(m) > 10 for m in decidida.motivos)
    assert decidida.metodo_pago_token is None
    assert _salidas(fabrica) == []  # nada que cobrar


async def test_ajustada_guarda_el_ajuste_y_espera_la_aceptacion_del_cliente(armar, fabrica):
    servicio = armar(cotizaciones=CotizacionesFalsas(cotizacion(monto=70_000_000)))
    susc, _ = await _iniciar(servicio)
    decidida = await servicio.decidir(susc.id)

    assert decidida.decision == "AJUSTADA"
    assert (decidida.estado_saga, decidida.paso_actual) == ("EN_PROGRESO", "DECISION")
    assert decidida.ajuste == {"monto_original": 70_000_000, "monto_asegurado": 50_000_000}
    assert _salidas(fabrica) == []  # no se cobra hasta que el cliente acepte (HU17)


async def test_derivada_registra_la_revision_asistida_y_deja_la_saga_en_espera(armar, fabrica):
    servicio = armar(perfiles=PerfilesFalsos(PerfilRiesgo.no_disponible("Perfilamiento no responde.")))
    susc, _ = await _iniciar(servicio)
    decidida = await servicio.decidir(susc.id)

    assert decidida.decision == "DERIVADA"
    assert (decidida.estado_saga, decidida.paso_actual) == ("EN_PROGRESO", "DECISION")
    (revision,) = servicio.revisiones(susc.id)
    assert revision.motivo_derivacion
    (salida,) = _salidas(fabrica)
    assert (salida.topic, salida.payload["event_type"]) == ("solventa.suscripcion.revision", "revision_asistida_solicitada")
    assert salida.payload["payload"].get("metodo_pago") is None
    assert decidida.metodo_pago_token == "tok_visa_123"  # la saga sigue viva: el token aún hace falta


async def test_decidir_dos_veces_es_inofensivo(armar, fabrica):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)
    assert await servicio.decidir(susc.id) is None
    assert len(_salidas(fabrica)) == 1


async def test_el_barrido_retoma_las_sagas_que_quedaron_sin_decidir(armar, fabrica):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    assert await servicio.retomar_pendientes(espera_s=30) == 0  # recién creada: todavía no
    # El reloj del servicio avanza más que la espera.
    tarde = armar()
    tarde._reloj = lambda: AHORA + timedelta(seconds=60)
    assert await tarde.retomar_pendientes(espera_s=30) == 1
    with fabrica() as s:
        assert repo.obtener(s, susc.id).decision == "APROBADA"
