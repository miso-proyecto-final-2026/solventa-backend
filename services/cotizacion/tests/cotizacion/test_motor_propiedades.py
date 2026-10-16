"""Propiedades del motor con Hypothesis. Los generadores salen de las propias
reglas, así que también cubren cualquier producto que se agregue después."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.cotizacion.dominio import PerfilUnificado
from app.cotizacion.motor import calcular_prima
from app.cotizacion.reglas import cargar_reglas

REGLAS = cargar_reglas("v1")
NOMBRES = sorted(REGLAS.productos)

# Valores de señal que recorren todos los tramos de las tablas (incluidos los bordes).
VALORES_SENAL = {
    "ratio_endeudamiento": st.floats(min_value=0, max_value=1.5, allow_nan=False),
    "reportes_negativos": st.integers(min_value=0, max_value=10),
    "ingresos_mensuales_cop": st.integers(min_value=0, max_value=30_000_000),
}


@st.composite
def datos_validos(draw, producto: str) -> dict:
    datos = {}
    for nombre, campo in REGLAS.productos[producto].campos.items():
        if campo.tipo == "entero":
            datos[nombre] = draw(st.integers(min_value=campo.min, max_value=campo.max))
        else:
            datos[nombre] = draw(st.sampled_from(campo.valores))
    return datos


@st.composite
def senales(draw, producto: str) -> dict:
    nombres = list(REGLAS.productos[producto].senales)
    presentes = draw(st.lists(st.sampled_from(nombres), unique=True)) if nombres else []
    return {n: draw(VALORES_SENAL[n]) for n in presentes}


@st.composite
def escenario(draw):
    producto = draw(st.sampled_from(NOMBRES))
    return producto, draw(datos_validos(producto)), draw(senales(producto))


@settings(max_examples=200, deadline=None)
@given(escenario(), st.booleans(), st.booleans())
def test_la_suma_de_componentes_es_la_prima_y_es_entera_no_negativa(
    esc, degradado, consiente
):
    producto, datos, sen = esc
    perfil = PerfilUnificado(
        "c", sen, degradado=degradado, con_consentimiento=consiente
    )

    r = calcular_prima(producto, datos, perfil, REGLAS)

    assert sum(c.monto for c in r.componentes) == r.prima_anual
    assert r.prima_anual > 0
    assert r.prima_anual == r.prima_anual.to_integral_value()


@settings(max_examples=200, deadline=None)
@given(escenario(), st.booleans())
def test_determinismo(esc, degradado):
    producto, datos, sen = esc
    perfil = PerfilUnificado("c", sen, degradado=degradado)

    assert calcular_prima(producto, datos, perfil, REGLAS) == calcular_prima(
        producto, dict(datos), perfil, REGLAS
    )


@settings(max_examples=300, deadline=None)
@given(escenario(), st.data())
def test_mas_riesgo_nunca_baja_la_prima(esc, data):
    """Monotonicidad: al empeorar una señal (según su sentido) la prima no baja."""
    producto, datos, sen = esc
    tablas = REGLAS.productos[producto].senales
    nombre = data.draw(st.sampled_from(sorted(tablas)))
    a = data.draw(VALORES_SENAL[nombre])
    b = data.draw(VALORES_SENAL[nombre])
    menos, mas = (min(a, b), max(a, b))
    if tablas[nombre].sentido == "inverso":
        menos, mas = (
            mas,
            menos,
        )  # en señales inversas, el riesgo crece al bajar el valor

    p_menos = calcular_prima(
        producto, datos, PerfilUnificado("c", {**sen, nombre: menos}), REGLAS
    )
    p_mas = calcular_prima(
        producto, datos, PerfilUnificado("c", {**sen, nombre: mas}), REGLAS
    )

    assert p_mas.prima_anual >= p_menos.prima_anual


@settings(max_examples=200, deadline=None)
@given(escenario())
def test_degradado_nunca_es_mas_barato_que_el_perfil_sin_senales(esc):
    producto, datos, _ = esc

    normal = calcular_prima(producto, datos, PerfilUnificado("c"), REGLAS)
    degradado = calcular_prima(
        producto, datos, PerfilUnificado("c", degradado=True), REGLAS
    )

    assert degradado.prima_anual >= normal.prima_anual
    assert degradado.degradado


@settings(max_examples=200, deadline=None)
@given(escenario())
def test_mayor_valor_asegurado_no_baja_la_prima(esc):
    producto, datos, sen = esc
    campo = REGLAS.productos[producto].base.campo_valor
    spec = REGLAS.productos[producto].campos[campo]
    perfil = PerfilUnificado("c", sen)

    bajo = calcular_prima(producto, {**datos, campo: spec.min}, perfil, REGLAS)
    alto = calcular_prima(producto, {**datos, campo: spec.max}, perfil, REGLAS)

    assert alto.prima_anual >= bajo.prima_anual >= Decimal(0)
