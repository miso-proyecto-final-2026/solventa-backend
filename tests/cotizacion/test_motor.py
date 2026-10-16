from decimal import Decimal

import pytest

from app.cotizacion.dominio import AVISO_APROXIMADA, PerfilUnificado
from app.cotizacion.errores import ErrorValidacion
from app.cotizacion.motor import calcular_prima

VIDA = {"edad": 35, "suma_asegurada": 50_000_000}
DISP = {
    "tipo_dispositivo": "celular",
    "valor_dispositivo": 2_500_000,
    "antiguedad_meses": 8,
}

SIN_SENALES = PerfilUnificado("c1")
SENALES_ALTAS = PerfilUnificado(
    "c1",
    senales={
        "ratio_endeudamiento": 0.6,
        "reportes_negativos": 3,
        "ingresos_mensuales_cop": 1_000_000,
    },
)
SENALES_BUENAS = PerfilUnificado(
    "c1", senales={"ratio_endeudamiento": 0.1, "ingresos_mensuales_cop": 8_000_000}
)
DEGRADADO = PerfilUnificado("c1", senales=SENALES_ALTAS.senales, degradado=True)
SIN_CONSENTIMIENTO = PerfilUnificado("c1", con_consentimiento=False)


def _datos(base: dict, **cambios) -> dict:
    return {**base, **cambios}


# (producto, datos, perfil, prima_anual, prima_mensual) - calculados a mano desde v1.yaml
CASOS_DE_PRIMA = [
    pytest.param(
        "microseguro_vida", VIDA, SIN_SENALES, 175_000, 14_583, id="vida-base"
    ),
    pytest.param(
        "microseguro_vida",
        _datos(VIDA, edad=25),
        SIN_SENALES,
        157_500,
        13_125,
        id="vida-joven",
    ),
    pytest.param(
        "microseguro_vida",
        _datos(VIDA, edad=45),
        SIN_SENALES,
        218_750,
        18_229,
        id="vida-40s",
    ),
    pytest.param(
        "microseguro_vida",
        _datos(VIDA, edad=55),
        SIN_SENALES,
        280_000,
        23_333,
        id="vida-50s",
    ),
    pytest.param(
        "microseguro_vida",
        _datos(VIDA, edad=65),
        SIN_SENALES,
        367_500,
        30_625,
        id="vida-65",
    ),
    pytest.param(
        "microseguro_vida",
        _datos(VIDA, suma_asegurada=5_000_000),
        SIN_SENALES,
        60_000,
        5_000,
        id="vida-prima-minima",
    ),
    pytest.param(
        "microseguro_vida",
        VIDA,
        SENALES_ALTAS,
        324_844,
        27_070,
        id="vida-senales-altas",
    ),
    pytest.param(
        "microseguro_vida",
        VIDA,
        SENALES_BUENAS,
        157_938,
        13_162,
        id="vida-senales-buenas-redondeo",
    ),
    pytest.param(
        "microseguro_vida",
        VIDA,
        DEGRADADO,
        201_250,
        16_771,
        id="vida-degradado-ignora-senales",
    ),
    pytest.param(
        "microseguro_vida",
        VIDA,
        SIN_CONSENTIMIENTO,
        175_000,
        14_583,
        id="vida-sin-consentimiento",
    ),
    pytest.param(
        "proteccion_dispositivos", DISP, SIN_SENALES, 135_844, 11_320, id="disp-celular"
    ),
    pytest.param(
        "proteccion_dispositivos",
        _datos(DISP, tipo_dispositivo="portatil", antiguedad_meses=30),
        SIN_SENALES,
        147_656,
        12_305,
        id="disp-portatil-antiguo",
    ),
    pytest.param(
        "proteccion_dispositivos",
        _datos(
            DISP,
            tipo_dispositivo="tableta",
            valor_dispositivo=300_000,
            antiguedad_meses=0,
        ),
        SIN_SENALES,
        37_800,
        3_150,
        id="disp-prima-minima",
    ),
    pytest.param(
        "proteccion_dispositivos", DISP, DEGRADADO, 156_221, 13_018, id="disp-degradado"
    ),
]


@pytest.mark.parametrize(
    ("producto", "datos", "perfil", "anual", "mensual"), CASOS_DE_PRIMA
)
def test_prima_por_producto_y_combinacion_de_senales(
    reglas, producto, datos, perfil, anual, mensual
):
    r = calcular_prima(producto, datos, perfil, reglas)

    assert r.prima_anual == Decimal(anual)
    assert r.prima_mensual == Decimal(mensual)
    assert sum(c.monto for c in r.componentes) == r.prima_anual
    assert (r.moneda, r.version_reglas, r.vigencia_dias) == ("COP", reglas.version, 30)


def test_coberturas_con_monto_y_deducible_por_producto(reglas):
    vida = calcular_prima("microseguro_vida", VIDA, SIN_SENALES, reglas)
    disp = calcular_prima("proteccion_dispositivos", DISP, SIN_SENALES, reglas)

    assert [(c.tipo, int(c.monto), int(c.deducible)) for c in vida.coberturas] == [
        ("muerte", 50_000_000, 0),
        ("incapacidad_total", 25_000_000, 0),
    ]
    assert [(c.tipo, int(c.monto), int(c.deducible)) for c in disp.coberturas] == [
        ("dano_accidental", 2_500_000, 250_000),
        ("robo", 2_500_000, 500_000),
    ]


def test_senales_ajustan_el_factor_de_riesgo(reglas):
    r = calcular_prima("microseguro_vida", VIDA, SENALES_ALTAS, reglas)

    ajuste_riesgo = next(c for c in r.componentes if c.concepto == "ajuste_riesgo")
    assert ajuste_riesgo.monto == Decimal(149_844)  # 175.000 * 1,85625 - 175.000
    assert not r.degradado
    assert not r.aproximada


def test_sin_senales_se_usa_el_factor_base_del_producto(reglas):
    r = calcular_prima("proteccion_dispositivos", DISP, SIN_SENALES, reglas)

    # factor_riesgo_base = 1,05 sobre 129.375 (base 112.500 * 1,15)
    ajuste_riesgo = next(c for c in r.componentes if c.concepto == "ajuste_riesgo")
    assert ajuste_riesgo.monto == Decimal(6_469)


def test_perfil_degradado_aplica_recargo_conservador_y_se_marca(reglas):
    r = calcular_prima("microseguro_vida", VIDA, DEGRADADO, reglas)

    recargo = next(c for c in r.componentes if c.concepto == "recargo_degradado")
    assert r.degradado is True
    assert recargo.monto == Decimal(26_250)  # 15 % de 175.000
    assert r.aviso is None


def test_sin_consentimiento_es_aproximada_sin_recargo(reglas):
    r = calcular_prima("microseguro_vida", VIDA, SIN_CONSENTIMIENTO, reglas)

    assert r.aproximada is True
    assert r.degradado is False
    assert r.aviso == AVISO_APROXIMADA
    assert (
        next(c for c in r.componentes if c.concepto == "recargo_degradado").monto == 0
    )


def test_determinismo_misma_entrada_misma_prima(reglas):
    corridas = {
        calcular_prima("microseguro_vida", VIDA, SENALES_ALTAS, reglas)
        for _ in range(50)
    }

    assert len(corridas) == 1


def test_el_motor_no_modifica_sus_entradas(reglas):
    datos = dict(VIDA)
    calcular_prima("microseguro_vida", datos, SENALES_ALTAS, reglas)

    assert datos == VIDA


# ---- validación de entrada (criterio 6): error con el campo, sin calcular ----


def _campos(excinfo) -> dict[str, str]:
    return {d.campo: d.motivo for d in excinfo.value.detalles}


def test_producto_inexistente(reglas):
    with pytest.raises(ErrorValidacion) as e:
        calcular_prima("seguro_de_barcos", VIDA, SIN_SENALES, reglas)

    assert "producto" in _campos(e)
    assert "microseguro_vida" in _campos(e)["producto"]


@pytest.mark.parametrize(
    ("datos", "campo", "texto"),
    [
        ({"suma_asegurada": 50_000_000}, "datos.edad", "requerido"),
        (_datos(VIDA, edad=None), "datos.edad", "requerido"),
        (_datos(VIDA, edad=17), "datos.edad", "mayor o igual a 18"),
        (_datos(VIDA, edad=66), "datos.edad", "menor o igual a 65"),
        (_datos(VIDA, edad="35"), "datos.edad", "entero"),
        (_datos(VIDA, edad=35.5), "datos.edad", "entero"),
        (_datos(VIDA, edad=True), "datos.edad", "entero"),
        (
            _datos(VIDA, suma_asegurada=4_999_999),
            "datos.suma_asegurada",
            "mayor o igual a",
        ),
        (
            _datos(VIDA, suma_asegurada=100_000_001),
            "datos.suma_asegurada",
            "menor o igual a",
        ),
        (_datos(VIDA, color="azul"), "datos.color", "no reconocido"),
    ],
)
def test_datos_incompletos_o_fuera_de_rango(reglas, datos, campo, texto):
    with pytest.raises(ErrorValidacion) as e:
        calcular_prima("microseguro_vida", datos, SIN_SENALES, reglas)

    assert texto in _campos(e)[campo]


def test_valor_no_permitido_en_campo_enumerado(reglas):
    with pytest.raises(ErrorValidacion) as e:
        calcular_prima(
            "proteccion_dispositivos",
            _datos(DISP, tipo_dispositivo="cafetera"),
            SIN_SENALES,
            reglas,
        )

    assert "celular, portatil, tableta" in _campos(e)["datos.tipo_dispositivo"]


def test_reporta_todos_los_errores_juntos(reglas):
    with pytest.raises(ErrorValidacion) as e:
        calcular_prima("microseguro_vida", {"edad": 10}, SIN_SENALES, reglas)

    assert set(_campos(e)) == {"datos.edad", "datos.suma_asegurada"}


@pytest.mark.parametrize("edad", [18, 65])
def test_limites_validos_de_rango_se_aceptan(reglas, edad):
    r = calcular_prima("microseguro_vida", _datos(VIDA, edad=edad), SIN_SENALES, reglas)

    assert r.prima_anual > 0
