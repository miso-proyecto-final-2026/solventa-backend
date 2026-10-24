"""Persistencia de la saga: idempotencia por clave, cotización de un solo uso y cola de salida de eventos."""

from datetime import UTC, datetime, timedelta

import pytest
from app.suscripcion import repositorio as repo
from sqlalchemy.exc import IntegrityError

AHORA = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _nueva(sesion, *, id="s1", cotizacion="c1", cliente="cli-1", clave="k1", **extra):
    return repo.nueva_suscripcion(
        sesion,
        id=id,
        cotizacion_id=cotizacion,
        cliente_id=cliente,
        clave_idempotencia=clave,
        producto="microseguro_vida",
        moneda="COP",
        monto_asegurado=50_000_000,
        prima_mensual=14_583,
        prima_anual=175_000,
        ahora=AHORA,
        **extra,
    )


def test_una_suscripcion_nueva_arranca_en_progreso_en_la_decision(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
    with fabrica() as s:
        susc = repo.obtener(s, "s1")

    assert (susc.estado_saga, susc.paso_actual, susc.decision) == ("EN_PROGRESO", "DECISION", None)
    assert susc.motivos == [] and susc.monto_asegurado == 50_000_000 and susc.prima_anual == 175_000


def test_la_misma_clave_del_mismo_cliente_no_se_puede_repetir(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
    with pytest.raises(IntegrityError), fabrica() as s, s.begin():
        _nueva(s, id="s2", cotizacion="c2")  # misma clave, otra cotización


def test_otro_cliente_puede_usar_la_misma_clave(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
        _nueva(s, id="s2", cotizacion="c2", cliente="cli-2")


def test_se_busca_por_cliente_y_clave_y_no_por_la_clave_sola(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
    with fabrica() as s:
        assert repo.buscar_por_clave(s, "cli-1", "k1").id == "s1"
        assert repo.buscar_por_clave(s, "cli-2", "k1") is None


def test_una_cotizacion_solo_se_puede_suscribir_una_vez(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
    with pytest.raises(IntegrityError), fabrica() as s, s.begin():
        _nueva(s, id="s2", clave="k2")  # misma cotización, otra clave


@pytest.mark.parametrize("estado", ["RECHAZADA", "COMPENSADA"])
def test_una_suscripcion_rechazada_o_compensada_libera_la_cotizacion(fabrica, estado):
    with fabrica() as s, s.begin():
        susc = _nueva(s)
        susc.estado_saga = estado
    with fabrica() as s, s.begin():
        _nueva(s, id="s2", clave="k2")  # no falla


def test_se_encuentra_la_suscripcion_activa_de_una_cotizacion(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s)
    with fabrica() as s:
        assert repo.buscar_activa_por_cotizacion(s, "c1").id == "s1"
        assert repo.buscar_activa_por_cotizacion(s, "otra") is None


def test_la_decision_deja_motivos_ajuste_version_y_duracion(fabrica):
    with fabrica() as s, s.begin():
        susc = _nueva(s)
        repo.registrar_decision(
            s, susc, decision="AJUSTADA", estado_saga="EN_PROGRESO", paso_actual="DECISION",
            motivos=["monto alto"], ajuste={"monto_original": 70, "monto_asegurado": 50},
            reglas_version="2026.10.1", ahora=AHORA + timedelta(milliseconds=120),
        )
    with fabrica() as s:
        susc = repo.obtener(s, "s1")

    assert susc.decision == "AJUSTADA" and susc.motivos == ["monto alto"] and susc.ajuste["monto_asegurado"] == 50
    assert susc.reglas_version == "2026.10.1" and susc.duracion_decision_ms == 120


def test_una_derivacion_crea_la_revision_asistida_con_su_motivo(fabrica):
    with fabrica() as s, s.begin():
        susc = _nueva(s)
        repo.crear_revision_asistida(s, susc.id, "Monto sobre el límite automático", AHORA)
    with fabrica() as s:
        (revision,) = repo.revisiones_de(s, "s1")

    assert revision.motivo_derivacion.startswith("Monto") and revision.decision is None and revision.asesor_id is None


def test_solo_las_suscripciones_sin_decision_y_antiguas_estan_pendientes(fabrica):
    with fabrica() as s, s.begin():
        _nueva(s, id="vieja", cotizacion="c1", clave="k1")
        repo.registrar_decision(
            s, _nueva(s, id="decidida", cotizacion="c2", clave="k2"), decision="APROBADA", estado_saga="EN_PROGRESO",
            paso_actual="COBRO", motivos=[], ajuste=None, reglas_version="v", ahora=AHORA,
        )
    with fabrica() as s, s.begin():
        nueva = _nueva(s, id="reciente", cotizacion="c3", clave="k3")
        nueva.created_at = AHORA + timedelta(seconds=60)
    with fabrica() as s:
        ids = [x.id for x in repo.pendientes_de_decision(s, antes_de=AHORA + timedelta(seconds=30), limite=10)]

    assert ids == ["vieja"]


def test_la_cola_de_salida_se_lee_en_orden_y_se_marca_publicada(fabrica):
    with fabrica() as s, s.begin():
        repo.encolar_evento(s, event_id="e1", topic="t", clave="k", payload={"n": 1}, ahora=AHORA)
        repo.encolar_evento(s, event_id="e2", topic="t", clave="k", payload={"n": 2}, ahora=AHORA)
    with fabrica() as s, s.begin():
        pendientes = repo.salidas_pendientes(s, limite=10)
        assert [p.event_id for p in pendientes] == ["e1", "e2"]
        repo.marcar_publicado(pendientes[0], payload_redactado={"n": 1}, ahora=AHORA)
    with fabrica() as s:
        assert [p.event_id for p in repo.salidas_pendientes(s, limite=10)] == ["e2"]


def test_un_fallo_de_publicacion_cuenta_el_intento_y_deja_el_evento_pendiente(fabrica):
    with fabrica() as s, s.begin():
        repo.encolar_evento(s, event_id="e1", topic="t", clave="k", payload={}, ahora=AHORA)
    with fabrica() as s, s.begin():
        (p,) = repo.salidas_pendientes(s, limite=10)
        repo.marcar_fallo(p, "sin bus")
    with fabrica() as s:
        (p,) = repo.salidas_pendientes(s, limite=10)

    assert p.intentos == 1 and p.ultimo_error == "sin bus" and p.publicado_en is None


def test_no_se_encola_dos_veces_el_mismo_evento(fabrica):
    with fabrica() as s, s.begin():
        repo.encolar_evento(s, event_id="e1", topic="t", clave="k", payload={}, ahora=AHORA)
    with pytest.raises(IntegrityError), fabrica() as s, s.begin():
        repo.encolar_evento(s, event_id="e1", topic="t", clave="k", payload={}, ahora=AHORA)
