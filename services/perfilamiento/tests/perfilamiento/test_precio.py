"""HU25: el cálculo del precio individualizado, sin base ni red (función pura)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.perfilamiento.precio import SumaFueraDeRango, calcular_oferta
from app.perfilamiento.reglas_precio import ErrorReglas, ReglasPrecio, cargar_reglas

REGLAS = cargar_reglas()

PERFIL = {
    "ingresos_mensuales_cop": {"valor": 4_500_000},
    "ratio_endeudamiento": {"valor": 0.32},
    "antiguedad_cuentas_anios": {"valor": 6},
    "score_crediticio": {"valor": 710},
    "reportes_negativos": {"valor": 0},
}


def oferta(campos=None, *, degradado=False, suma=None, reglas=REGLAS):
    return calcular_oferta(PERFIL if campos is None else campos, degradado=degradado, suma_asegurada=suma, reglas=reglas)


def con(**cambios):
    return {**PERFIL, **{k: {"valor": v} for k, v in cambios.items()}}


def sin(*nombres):
    return {k: v for k, v in PERFIL.items() if k not in nombres}


# --- Criterio 2: individualizado y determinista ----------------------------------------------------------


def test_el_mismo_perfil_produce_siempre_el_mismo_precio():
    assert oferta() == oferta()


def test_dos_perfiles_distintos_producen_precios_distintos():
    prudente = oferta(con(ratio_endeudamiento=0.10, score_crediticio=800, ingresos_mensuales_cop=9_000_000), suma=200_000_000)
    riesgoso = oferta(con(ratio_endeudamiento=0.60, score_crediticio=400, reportes_negativos=4), suma=200_000_000)

    assert riesgoso.prima_anual > prudente.prima_anual


def test_el_perfil_de_referencia_tiene_un_precio_conocido():
    o = oferta()

    # 6 años de cuentas = desarrollo (1.00); ningún factor mueve la base: 0.0018 × (4,5 M × 60 meses).
    assert (o.suma_asegurada, o.prima_anual, o.prima_mensual, o.etapa_de_vida) == (270_000_000, 486_000, 40_500, "desarrollo")


# --- Criterio 3: etapa de vida ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anios", "etapa"),
    [(0, "inicial"), (4.99, "inicial"), (5, "desarrollo"), (14.9, "desarrollo"), (15, "consolidacion"), (29, "consolidacion"), (30, "madurez"), (60, "madurez")],
)
def test_la_etapa_de_vida_sale_de_la_antiguedad_con_los_limites_del_archivo(anios, etapa):
    assert oferta(con(antiguedad_cuentas_anios=anios)).etapa_de_vida == etapa


def test_sin_antiguedad_la_etapa_es_sin_dato_y_no_mueve_el_precio():
    o = oferta(sin("antiguedad_cuentas_anios"))

    paso = next(p for p in o.pasos if p.paso == "etapa_de_vida")
    assert (o.etapa_de_vida, paso.sin_dato, paso.ajuste) == ("sin_dato", True, 0)


# --- Reglas por señal: valor interno, borde y fuera ------------------------------------------------------


@pytest.mark.parametrize(
    ("senal", "valor", "factor"),
    [
        ("ratio_endeudamiento", 0.10, "0.95"), ("ratio_endeudamiento", 0.20, "1.00"), ("ratio_endeudamiento", 0.349, "1.00"),
        ("ratio_endeudamiento", 0.35, "1.10"), ("ratio_endeudamiento", 0.50, "1.25"), ("ratio_endeudamiento", 1.0, "1.25"),
        ("reportes_negativos", 0, "1.00"), ("reportes_negativos", 1, "1.15"), ("reportes_negativos", 3, "1.35"),
        ("ingresos_mensuales_cop", 1_999_999, "1.10"), ("ingresos_mensuales_cop", 2_000_000, "1.00"), ("ingresos_mensuales_cop", 6_000_000, "0.95"),
        ("score_crediticio", 499, "1.20"), ("score_crediticio", 500, "1.05"), ("score_crediticio", 750, "0.95"),
    ],
)
def test_cada_regla_aplica_su_factor_en_el_borde(senal, valor, factor):
    paso = next(p for p in oferta(con(**{senal: valor}), suma=100_000_000).pasos if p.nombre == senal)

    assert paso.factor == Decimal(factor)


def test_una_senal_ausente_no_mueve_el_precio_y_queda_anotada():
    o = oferta(sin("score_crediticio"))

    paso = next(p for p in o.pasos if p.nombre == "score_crediticio")
    assert (paso.sin_dato, paso.factor, paso.ajuste, paso.valor_perfil) == (True, Decimal(1), 0, None)


# --- Criterio 9: moneda y redondeo ------------------------------------------------------------------------


def test_la_base_mas_los_ajustes_suman_exactamente_la_prima_anual():
    o = oferta(con(ratio_endeudamiento=0.55, reportes_negativos=2, score_crediticio=520, antiguedad_cuentas_anios=20), degradado=True, suma=123_456_789)

    assert sum(p.ajuste for p in o.pasos) == o.prima_anual
    assert o.prima_anual == o.pasos[-1].acumulado
    assert o.prima_anual == o.prima_anual.to_integral_value() and o.moneda == "COP"


def test_el_redondeo_es_half_up_en_cada_paso():
    # 0.0018 × 20_250_000 = 36_450; base mínima 180_000 domina: sube a la mínima, sin decimales.
    assert oferta(suma=20_250_000).pasos[0].ajuste == 180_000


def test_la_prima_minima_se_aplica_con_sumas_chicas():
    o = oferta(suma=20_000_000)

    assert o.pasos[0].acumulado == REGLAS.base.prima_minima


def test_el_recargo_por_datos_de_respaldo_solo_aplica_a_un_perfil_degradado():
    normal, degradado = oferta(degradado=False), oferta(degradado=True)

    recargo = degradado.pasos[-1]
    assert (recargo.paso, recargo.factor) == ("recargo_degradado", Decimal("0.15"))
    assert degradado.prima_anual == normal.prima_anual + (normal.prima_anual * Decimal("0.15")).quantize(Decimal(1), rounding="ROUND_HALF_UP")
    assert all(p.paso != "recargo_degradado" for p in normal.pasos)


# --- Suma asegurada ---------------------------------------------------------------------------------------


def test_sin_suma_se_usa_el_ingreso_por_los_meses_de_las_reglas_dentro_de_los_limites():
    assert oferta().suma_asegurada == Decimal(270_000_000)
    assert oferta(con(ingresos_mensuales_cop=100_000)).suma_asegurada == REGLAS.suma_asegurada.minimo
    assert oferta(con(ingresos_mensuales_cop=50_000_000)).suma_asegurada == REGLAS.suma_asegurada.maximo


def test_sin_ingresos_se_usa_la_suma_por_defecto():
    assert oferta(sin("ingresos_mensuales_cop")).suma_asegurada == REGLAS.suma_asegurada.por_defecto


@pytest.mark.parametrize("suma", [19_999_999, 800_000_001])
def test_una_suma_fuera_de_los_limites_se_rechaza(suma):
    with pytest.raises(SumaFueraDeRango):
        oferta(suma=suma)


def test_las_coberturas_salen_de_las_reglas():
    cobertura = {c["tipo"]: c["monto_cubierto"] for c in oferta(suma=100_000_000).coberturas}

    assert cobertura == {"muerte": 100_000_000, "incapacidad_total_permanente": 100_000_000}


# --- Propiedad: el precio no decrece cuando el riesgo aumenta --------------------------------------------


@settings(max_examples=150, deadline=None)
@given(
    base=st.floats(min_value=0, max_value=1, allow_nan=False),
    mas=st.floats(min_value=0, max_value=1, allow_nan=False),
    reportes=st.integers(0, 10),
    mas_reportes=st.integers(0, 10),
    score=st.integers(0, 1000),
    menos_score=st.integers(0, 1000),
    anios=st.floats(min_value=0, max_value=60, allow_nan=False),
    mas_anios=st.floats(min_value=0, max_value=60, allow_nan=False),
    suma=st.integers(20_000_000, 800_000_000),
)
def test_el_precio_no_baja_cuando_el_riesgo_sube(base, mas, reportes, mas_reportes, score, menos_score, anios, mas_anios, suma):
    antes = con(ratio_endeudamiento=base, reportes_negativos=reportes, score_crediticio=score, antiguedad_cuentas_anios=anios)
    despues = con(
        ratio_endeudamiento=min(1, base + mas), reportes_negativos=reportes + mas_reportes,
        score_crediticio=max(0, score - menos_score), antiguedad_cuentas_anios=anios + mas_anios,
    )

    assert oferta(despues, suma=suma).prima_anual >= oferta(antes, suma=suma).prima_anual


# --- Criterio 4: reglas versionadas en un archivo y validadas al cargar -----------------------------------


def test_las_reglas_traen_su_version_y_estan_en_un_archivo():
    assert REGLAS.version == "2026.11.1-prototipo" and REGLAS.producto == "vida_hipotecario"
    assert oferta().version_reglas == REGLAS.version


def _con_cambio(tmp_path, anterior, nuevo):
    from app.perfilamiento.reglas_precio import ARCHIVO_POR_DEFECTO, DIRECTORIO_REGLAS

    texto = (DIRECTORIO_REGLAS / ARCHIVO_POR_DEFECTO).read_text(encoding="utf-8")
    assert anterior in texto
    ruta = tmp_path / "reglas.yaml"
    ruta.write_text(texto.replace(anterior, nuevo, 1), encoding="utf-8")
    return ruta


def test_otra_version_del_archivo_cambia_el_precio_sin_tocar_el_codigo(tmp_path):
    ruta = _con_cambio(tmp_path, "tasa_anual: 0.0018", "tasa_anual: 0.0036")
    reglas = cargar_reglas(ruta)

    assert oferta(reglas=reglas).prima_anual == 2 * oferta().prima_anual


def test_una_regla_que_baja_el_precio_cuando_el_riesgo_sube_no_carga(tmp_path):
    ruta = _con_cambio(tmp_path, "{desde: null, hasta: 0.20, factor: 0.95}", "{desde: null, hasta: 0.20, factor: 1.50}")

    with pytest.raises(ErrorReglas, match="no decrecientes"):
        cargar_reglas(ruta)


def test_unos_rangos_con_huecos_no_cargan(tmp_path):
    ruta = _con_cambio(tmp_path, "{desde: 5, hasta: 15,", "{desde: 6, hasta: 15,")

    with pytest.raises(ErrorReglas, match="contiguos"):
        cargar_reglas(ruta)


def test_un_archivo_inexistente_falla_con_un_error_claro(tmp_path):
    with pytest.raises(ErrorReglas):
        cargar_reglas(tmp_path / "no-existe.yaml")


def test_los_modelos_de_reglas_son_inmutables():
    assert isinstance(REGLAS, ReglasPrecio)
    with pytest.raises(Exception):  # noqa: B017  (pydantic frozen)
        REGLAS.version = "otra"
