"""Reglas de disparo y su evaluación pura (HU28, criterios 2, 3 y 8)."""

import pytest
from app.siniestros.parametrico import (
    CORRESPONDE_PAGO,
    NO_CORRESPONDE,
    ReglasInvalidas,
    cargar_reglas,
    evaluar,
)


@pytest.fixture(scope="module")
def reglas():
    return cargar_reglas()


def test_el_archivo_de_reglas_versionado_carga_y_declara_su_version(reglas):
    assert reglas.version == "v1"
    assert {"parametrico_clima", "parametrico_vuelo"} <= reglas.productos


def test_un_sismo_en_la_zona_con_magnitud_suficiente_corresponde_pago(reglas):
    regla = reglas.regla("parametrico_clima", "sismo")

    decision = evaluar(regla, {"magnitud": 6.4}, "bogota", "bogota")

    assert decision.resultado == CORRESPONDE_PAGO and decision.monto_indemnizacion == 5_000_000
    assert decision.valores["magnitud"] == {"observado": 6.4, "operador": ">=", "umbral": 6.0}


def test_la_magnitud_justo_en_el_umbral_cumple_y_justo_debajo_no(reglas):
    regla = reglas.regla("parametrico_clima", "sismo")

    assert evaluar(regla, {"magnitud": 6.0}, "bogota", "bogota").corresponde_pago
    debajo = evaluar(regla, {"magnitud": 5.9}, "bogota", "bogota")
    assert debajo.resultado == NO_CORRESPONDE and debajo.motivo == "condicion_no_cumplida:magnitud"
    assert debajo.valores["magnitud"]["observado"] == 5.9  # el valor evaluado queda para auditoría


def test_un_evento_fuera_de_la_zona_asegurada_no_corresponde(reglas):
    decision = evaluar(reglas.regla("parametrico_clima", "sismo"), {"magnitud": 7.0}, "cali", "bogota")

    assert decision.motivo == "fuera_de_la_zona_asegurada" and not decision.corresponde_pago


def test_una_poliza_sin_zona_no_cobra_un_riesgo_que_exige_zona(reglas):
    decision = evaluar(reglas.regla("parametrico_clima", "sismo"), {"magnitud": 7.0}, "bogota", None)

    assert decision.motivo == "poliza_sin_zona_asegurada"


def test_un_dato_faltante_o_no_numerico_no_corresponde(reglas):
    regla = reglas.regla("parametrico_vuelo", "vuelo_retrasado")

    assert evaluar(regla, {}, None, None).motivo == "dato_faltante:retraso_minutos"
    assert evaluar(regla, {"retraso_minutos": "mucho"}, None, None).motivo == "dato_faltante:retraso_minutos"
    assert evaluar(regla, {"retraso_minutos": True}, None, None).motivo == "dato_faltante:retraso_minutos"


def test_una_condicion_con_menor_o_igual_sirve_para_la_sequia(reglas):
    regla = reglas.regla("parametrico_clima", "sequia")

    assert evaluar(regla, {"precipitacion_mm_30d": 12}, "meta", "meta").corresponde_pago
    assert not evaluar(regla, {"precipitacion_mm_30d": 45}, "meta", "meta").corresponde_pago


def test_una_regla_sin_condiciones_paga_siempre_que_el_evento_llegue(reglas):
    assert evaluar(reglas.regla("parametrico_vuelo", "vuelo_cancelado"), {"vuelo": "AV123"}, None, None).corresponde_pago


@pytest.mark.parametrize(
    "contenido, fragmento",
    [
        ("productos: {}", "version"),
        ('version: "v1"\nproductos: {}', "productos"),
        ('version: "v1"\nproductos: {p: {reglas: []}}', "no tiene reglas"),
        (
            'version: "v1"\nproductos: {p: {reglas: [{id: a, tipo_evento: marte, monto_indemnizacion: 1}]}}',
            "tipo_evento",
        ),
        (
            'version: "v1"\nproductos: {p: {reglas: [{id: a, tipo_evento: sismo, monto_indemnizacion: 0}]}}',
            "monto_indemnizacion",
        ),
        (
            (
                'version: "v1"\nproductos: {p: {reglas: [{id: a, tipo_evento: sismo, monto_indemnizacion: 1,'
                " condiciones: [{campo: m, operador: '~', umbral: 1}]}]}}"
            ),
            "operador",
        ),
        (
            (
                'version: "v1"\nproductos: {p: {reglas: ['
                "{id: a, tipo_evento: sismo, monto_indemnizacion: 1}, {id: b, tipo_evento: sismo, monto_indemnizacion: 1}]}}"
            ),
            "mismo tipo de evento",
        ),
    ],
)
def test_un_archivo_de_reglas_defectuoso_impide_arrancar(tmp_path, contenido, fragmento):
    ruta = tmp_path / "reglas.yaml"
    ruta.write_text(contenido, encoding="utf-8")

    with pytest.raises(ReglasInvalidas, match=fragmento):
        cargar_reglas(ruta)


def test_un_archivo_inexistente_impide_arrancar(tmp_path):
    with pytest.raises(ReglasInvalidas):
        cargar_reglas(tmp_path / "no-existe.yaml")
