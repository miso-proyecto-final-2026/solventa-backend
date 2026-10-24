"""Validación del evento de entrada (criterios 2 y 3), sin HTTP ni base."""

import pytest
from app.siniestros.dominio import TIPOS_EVENTO, validar

from tests.siniestros.conftest import AHORA, FUENTE, evento


def _campos(defectos):
    return {d.campo for d in defectos}


def test_un_evento_completo_es_valido():
    valido, defectos, evento_id = validar(evento(), FUENTE, AHORA)

    assert defectos == []
    assert valido is not None and valido.evento_id == evento_id == "evt-1"


@pytest.mark.parametrize("tipo", sorted(TIPOS_EVENTO))
def test_se_aceptan_los_tipos_de_vuelos_y_clima(tipo):
    assert validar(evento(tipo=tipo), FUENTE, AHORA)[0] is not None


@pytest.mark.parametrize("campo", ["evento_id", "tipo", "fuente", "ocurrido_en", "datos"])
def test_cada_campo_obligatorio_faltante_se_reporta(campo):
    _, defectos, _ = validar(evento(**{campo: None}), FUENTE, AHORA)

    assert campo in _campos(defectos)


def test_un_tipo_desconocido_se_rechaza():
    assert "tipo" in _campos(validar(evento(tipo="meteorito"), FUENTE, AHORA)[1])


def test_una_fuente_distinta_a_la_de_la_llave_es_desconocida():
    assert "fuente" in _campos(validar(evento(fuente="otro"), FUENTE, AHORA)[1])


@pytest.mark.parametrize("fecha", ["ayer", "2026-10-23T14:05:00", 12345])
def test_la_fecha_debe_ser_iso_con_zona(fecha):
    assert "ocurrido_en" in _campos(validar(evento(ocurrido_en=fecha), FUENTE, AHORA)[1])


def test_la_fecha_en_el_futuro_se_rechaza_pero_se_tolera_un_reloj_adelantado():
    assert "ocurrido_en" in _campos(
        validar(evento(ocurrido_en="2026-10-23T16:00:00Z"), FUENTE, AHORA)[1]
    )
    assert validar(evento(ocurrido_en="2026-10-23T15:03:00Z"), FUENTE, AHORA)[0] is not None


def test_los_datos_no_pueden_ser_vacios_ni_enormes():
    assert "datos" in _campos(validar(evento(datos={}), FUENTE, AHORA)[1])
    assert "datos" in _campos(validar(evento(datos={"x": "a" * 9000}), FUENTE, AHORA)[1])


@pytest.mark.parametrize("identificador", ["con espacio", "a" * 65, "../x", 7])
def test_el_identificador_del_evento_es_acotado(identificador):
    assert "evento_id" in _campos(validar(evento(evento_id=identificador), FUENTE, AHORA)[1])


def test_el_cuerpo_debe_ser_un_objeto():
    assert _campos(validar([1, 2], FUENTE, AHORA)[1]) == {"cuerpo"}


def test_los_defectos_no_exponen_los_datos_recibidos():
    _, defectos, _ = validar(evento(tipo="x", datos={"secreto": "123"}), FUENTE, AHORA)

    assert "123" not in " ".join(d.motivo for d in defectos)
