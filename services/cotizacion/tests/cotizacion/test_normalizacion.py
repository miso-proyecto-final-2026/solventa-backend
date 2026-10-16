"""Pruebas de la normalización al perfil canónico (HU10)."""

import json
import logging
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.cotizacion.normalizacion import (
    CAMPOS,
    NORMALIZADORES,
    MotivoAusencia,
    _mapeo,
    normalizar,
    validar,
)
from app.cotizacion.perfil import AdaptadorPerfilProveedores, unificar
from app.cotizacion.reglas import cargar_reglas
from app.proveedores.dominio import Fuente, PerfilCrudo, ResultadoFuente
from app.proveedores.simuladores import ModoFalla
from tests.proveedores.conftest import construir, redis  # noqa: F401  (fixtures reutilizadas)

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 17, 12, 5, tzinfo=UTC)

OF = {
    "customerId": "c1",
    "monthlyIncomeCop": 4_500_000,
    "debtRatio": 0.32,
    "accountsOpenYears": 6,
}
OD = {
    "documento_ref": "c1",
    "ocupacion": "empleado",
    "estrato": 3,
    "reportes_negativos": 0,
}


def _crudo(*fuentes: ResultadoFuente, por_defecto=False) -> PerfilCrudo:
    return PerfilCrudo("c1", list(fuentes), por_defecto=por_defecto)


def _of(payload=OF, **kw):
    return ResultadoFuente(Fuente.OPEN_FINANCE, payload, obtenido_en=T0, **kw)


def _od(payload=OD, **kw):
    return ResultadoFuente(Fuente.OPEN_DATA, payload, obtenido_en=T1, **kw)


def _valores(perfil):
    return {n: c.valor for n, c in perfil.campos.items()}


# --- criterio 1: esquema canónico con fuente y fecha por campo ---------------


def test_dos_fuentes_completas_dan_el_perfil_canonico_con_fuente_y_fecha():
    perfil = normalizar(_crudo(_of(), _od()))

    assert _valores(perfil) == {
        "ingresos_mensuales_cop": 4_500_000,
        "ratio_endeudamiento": 0.32,
        "antiguedad_cuentas_anios": 6,
        "estrato": 3,
        "reportes_negativos": 0,
    }
    assert perfil.campos["ratio_endeudamiento"].fuente is Fuente.OPEN_FINANCE
    assert perfil.campos["ratio_endeudamiento"].obtenido_en == T0
    assert perfil.campos["estrato"].fuente is Fuente.OPEN_DATA
    assert perfil.campos["estrato"].obtenido_en == T1
    assert perfil.completitud == 1.0
    assert perfil.ausentes == {}


# --- criterio 2: datos faltantes, sin inventar valores -----------------------


def test_una_sola_fuente_deja_los_demas_campos_ausentes_y_baja_la_completitud():
    perfil = normalizar(_crudo(_of()))

    assert set(perfil.campos) == {
        "ingresos_mensuales_cop",
        "ratio_endeudamiento",
        "antiguedad_cuentas_anios",
    }
    assert perfil.ausentes == {
        "estrato": MotivoAusencia.NO_REPORTADO,
        "reportes_negativos": MotivoAusencia.NO_REPORTADO,
    }
    assert perfil.completitud == 0.6


def test_campo_ausente_dentro_de_una_fuente_que_si_respondio():
    perfil = normalizar(_crudo(_of({"debtRatio": 0.4})))

    assert _valores(perfil) == {"ratio_endeudamiento": 0.4}
    assert perfil.ausentes["ingresos_mensuales_cop"] is MotivoAusencia.NO_REPORTADO


def test_fuente_con_perfil_por_defecto_no_aporta_valores_inventados():
    por_defecto = ResultadoFuente(
        Fuente.OPEN_DATA,
        {"datos_disponibles": False},
        degradado=True,
        por_defecto=True,
    )
    perfil = normalizar(_crudo(_of(), por_defecto))

    assert "estrato" not in perfil.campos
    assert perfil.ausentes["estrato"] is MotivoAusencia.SIN_DATOS
    assert perfil.degradado is True


def test_sin_fuentes_el_perfil_queda_vacio():
    perfil = normalizar(_crudo())

    assert perfil.campos == {}
    assert perfil.completitud == 0.0
    assert set(perfil.ausentes.values()) == {MotivoAusencia.NO_REPORTADO}


def test_sin_consentimiento_no_hay_campos_y_se_indica_el_motivo():
    perfil = normalizar(_crudo(por_defecto=True))

    assert perfil.con_consentimiento is False
    assert perfil.degradado is False
    assert perfil.campos == {}
    assert set(perfil.ausentes.values()) == {MotivoAusencia.SIN_CONSENTIMIENTO}


# --- criterio 3: unidades, formatos y rangos ---------------------------------


@pytest.mark.parametrize(
    ("campo", "bruto", "motivo"),
    [
        ("ratio_endeudamiento", 1.5, MotivoAusencia.FUERA_DE_RANGO),
        ("ratio_endeudamiento", -0.1, MotivoAusencia.FUERA_DE_RANGO),
        ("ingresos_mensuales_cop", -1, MotivoAusencia.FUERA_DE_RANGO),
        ("estrato", 9, MotivoAusencia.FUERA_DE_RANGO),
        ("estrato", 0, MotivoAusencia.FUERA_DE_RANGO),
        ("estrato", 2.5, MotivoAusencia.INVALIDO),
        ("reportes_negativos", "muchos", MotivoAusencia.INVALIDO),
        ("reportes_negativos", True, MotivoAusencia.INVALIDO),
        ("reportes_negativos", None, MotivoAusencia.INVALIDO),
        ("ratio_endeudamiento", float("nan"), MotivoAusencia.INVALIDO),
        ("ratio_endeudamiento", float("inf"), MotivoAusencia.INVALIDO),
    ],
)
def test_valores_invalidos_o_fuera_de_rango_se_descartan(campo, bruto, motivo):
    assert validar(CAMPOS[campo], bruto) is motivo


def test_enteros_representados_como_float_se_normalizan_a_entero():
    valor = validar(CAMPOS["estrato"], 3.0)

    assert valor == 3
    assert isinstance(valor, int)


def test_el_valor_descartado_no_entra_al_perfil_y_el_log_no_expone_el_valor(caplog):
    caplog.set_level(logging.WARNING)
    perfil = normalizar(_crudo(_of({"debtRatio": 7.77, "accountsOpenYears": 6})))

    assert "ratio_endeudamiento" not in perfil.campos
    assert perfil.ausentes["ratio_endeudamiento"] is MotivoAusencia.FUERA_DE_RANGO
    assert "ratio_endeudamiento" in caplog.text
    assert "7.77" not in caplog.text


def test_la_serializacion_usa_fechas_iso_8601():
    datos = normalizar(_crudo(_of())).a_dict()

    assert datos["campos"]["ratio_endeudamiento"]["obtenido_en"] == T0.isoformat()
    assert datos["campos"]["ratio_endeudamiento"]["fuente"] == "open_finance"
    assert json.loads(json.dumps(datos)) == datos


# --- criterio 4: conflicto entre fuentes -------------------------------------


def _con_estrato_en_ambas():
    """Open Data también reporta `ingresos_mensuales_cop` (caso de conflicto)."""
    normalizadores = {
        **NORMALIZADORES,
        Fuente.OPEN_DATA: _mapeo(
            {"ingreso_declarado": "ingresos_mensuales_cop", "estrato": "estrato"}
        ),
    }
    return normalizadores


def test_en_conflicto_prevalece_open_finance_y_se_registra_la_fuente_ganadora():
    crudo = _crudo(_of(), _od({"ingreso_declarado": 3_000_000, "estrato": 3}))

    perfil = normalizar(crudo, normalizadores=_con_estrato_en_ambas())

    campo = perfil.campos["ingresos_mensuales_cop"]
    assert (campo.valor, campo.fuente) == (4_500_000, Fuente.OPEN_FINANCE)
    assert [(c.campo, c.ganadora, c.descartadas) for c in perfil.conflictos] == [
        ("ingresos_mensuales_cop", Fuente.OPEN_FINANCE, (Fuente.OPEN_DATA,))
    ]


def test_el_orden_de_llegada_no_cambia_el_ganador():
    datos = {"ingreso_declarado": 3_000_000}
    a = normalizar(_crudo(_of(), _od(datos)), normalizadores=_con_estrato_en_ambas())
    b = normalizar(_crudo(_od(datos), _of()), normalizadores=_con_estrato_en_ambas())

    assert a.campos["ingresos_mensuales_cop"] == b.campos["ingresos_mensuales_cop"]


def test_si_ambas_fuentes_coinciden_no_hay_conflicto():
    crudo = _crudo(_of(), _od({"ingreso_declarado": 4_500_000}))

    perfil = normalizar(crudo, normalizadores=_con_estrato_en_ambas())

    assert perfil.conflictos == ()


def test_si_la_ganadora_trae_un_valor_invalido_se_usa_la_otra_fuente():
    crudo = _crudo(_of({"monthlyIncomeCop": -5}), _od({"ingreso_declarado": 3_000_000}))

    perfil = normalizar(crudo, normalizadores=_con_estrato_en_ambas())

    assert perfil.campos["ingresos_mensuales_cop"].fuente is Fuente.OPEN_DATA
    assert perfil.conflictos == ()


# --- criterio 5: perfil degradado --------------------------------------------


def test_el_respaldo_conserva_la_marca_degradado_pero_no_aporta_valores():
    perfil = normalizar(_crudo(_of(degradado=True, hit_cache=True), _od()))

    assert perfil.degradado is True
    assert perfil.hit_cache is False
    assert "ratio_endeudamiento" not in perfil.campos
    assert perfil.ausentes["ratio_endeudamiento"] is MotivoAusencia.DEGRADADO
    assert perfil.campos["estrato"].valor == 3


def test_unificar_propaga_degradado_y_no_entrega_senales_al_motor():
    unificado = unificar(_crudo(_of(degradado=True, hit_cache=True), _od()))

    assert unificado.degradado is True
    assert dict(unificado.senales) == {}


# --- criterio 7: aislamiento del proveedor -----------------------------------


def test_ningun_campo_del_proveedor_sale_del_perfil_canonico():
    crudo = _crudo(_of(), _od())
    datos = normalizar(crudo).a_dict()
    texto = json.dumps(datos)
    nombres_proveedor = {k for f in crudo.fuentes for k in f.payload} - set(CAMPOS)

    assert nombres_proveedor  # la prueba solo vale si hay nombres propios del proveedor
    assert not any(n in texto for n in nombres_proveedor)
    assert set(datos["campos"]) <= set(CAMPOS)


def test_campos_desconocidos_del_proveedor_se_ignoran():
    perfil = normalizar(_crudo(_of({**OF, "scoreInterno": 999})))

    assert set(perfil.campos) <= set(CAMPOS)


# --- criterio 6: determinismo (propiedades) ----------------------------------

_numeros = st.one_of(
    st.integers(-10, 10**10),
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.text(max_size=5),
    st.none(),
)
_payload_of = st.fixed_dictionaries(
    {},
    optional={
        "monthlyIncomeCop": _numeros,
        "debtRatio": _numeros,
        "accountsOpenYears": _numeros,
    },
)
_payload_od = st.fixed_dictionaries(
    {}, optional={"estrato": _numeros, "reportes_negativos": _numeros}
)


@given(_payload_of, _payload_od, st.booleans())
def test_la_misma_entrada_da_siempre_el_mismo_perfil(p_of, p_od, degradado):
    crudo = _crudo(_of(p_of, degradado=degradado), _od(p_od))

    assert normalizar(crudo) == normalizar(crudo)
    assert normalizar(crudo).a_dict() == normalizar(crudo).a_dict()


@given(_payload_of, _payload_od)
def test_todo_campo_entregado_cumple_el_esquema_y_cada_campo_esta_o_ausente(p_of, p_od):
    perfil = normalizar(_crudo(_of(p_of), _od(p_od)))

    assert set(perfil.campos) | set(perfil.ausentes) == set(CAMPOS)
    assert not set(perfil.campos) & set(perfil.ausentes)
    for nombre, campo in perfil.campos.items():
        assert validar(CAMPOS[nombre], campo.valor) == campo.valor
    assert 0.0 <= perfil.completitud <= 1.0


@given(_payload_of, _payload_od)
def test_normalizar_es_idempotente_sobre_valores_ya_canonicos(p_of, p_od):
    """Normalizar los valores ya normalizados (con nombres canónicos) no los cambia."""
    primera = normalizar(_crudo(_of(p_of), _od(p_od)))
    canonicos = {n: c.valor for n, c in primera.campos.items()}
    identidad = {f: _mapeo({n: n for n in CAMPOS}) for f in Fuente}

    segunda = normalizar(_crudo(_of(canonicos)), normalizadores=identidad)

    assert _valores(segunda) == _valores(primera)


# --- contrato con el consumidor (motor de rating, HU12) ----------------------


def test_todas_las_senales_de_las_reglas_existen_en_el_esquema_canonico():
    reglas = cargar_reglas("v1")
    usadas = {s for p in reglas.productos.values() for s in p.senales}

    assert usadas
    assert usadas <= set(CAMPOS)


def test_unificar_entrega_al_motor_el_mismo_perfil_que_el_canonico():
    crudo = _crudo(_of(), _od())

    unificado = unificar(crudo)

    assert dict(unificado.senales) == _valores(normalizar(crudo))
    assert unificado.completitud == 1.0
    assert unificado.fuentes == ("open_finance", "open_data")


# --- módulo: HU09 (servicio real con simuladores) -> HU10 --------------------


async def test_flujo_completo_desde_el_servicio_de_proveedores(construir):  # noqa: F811
    servicio, _, _ = construir()

    crudo = await servicio.obtener_perfil("c1")
    perfil = normalizar(crudo)

    assert perfil.completitud == 1.0
    assert all(c.obtenido_en is not None for c in perfil.campos.values())
    assert all(c.obtenido_en.tzinfo is not None for c in perfil.campos.values())


async def test_la_fecha_de_obtencion_sobrevive_al_pasar_por_la_cache(construir):  # noqa: F811
    servicio, _, _ = construir()

    fresco = normalizar(await servicio.obtener_perfil("c1"))
    cacheado = normalizar(await servicio.obtener_perfil("c1"))

    assert cacheado.hit_cache is True
    assert cacheado.campos == fresco.campos  # misma fuente, mismo valor, misma fecha


async def test_la_clave_interna_de_fecha_no_llega_a_los_normalizadores(construir):  # noqa: F811
    servicio, _, _ = construir()
    await servicio.obtener_perfil("c1")

    crudo = await servicio.obtener_perfil("c1")

    assert all("__obtenido_en" not in f.payload for f in crudo.fuentes)


async def test_con_open_finance_caido_el_perfil_queda_degradado_y_sin_sus_campos(
    construir,  # noqa: F811
):
    servicio, _, _ = construir(modo_of=ModoFalla.ERROR_5XX)

    perfil = normalizar(await servicio.obtener_perfil("c1"))

    assert perfil.degradado is True
    assert perfil.campos["estrato"].fuente is Fuente.OPEN_DATA
    assert "ratio_endeudamiento" not in perfil.campos
    assert perfil.ausentes["ratio_endeudamiento"] is MotivoAusencia.SIN_DATOS


async def test_el_adaptador_entrega_al_motor_la_completitud(construir):  # noqa: F811
    servicio, _, _ = construir()

    perfil = await AdaptadorPerfilProveedores(servicio).obtener("c1")

    assert perfil.completitud == 1.0


async def test_una_fecha_corrupta_en_cache_no_rompe_el_perfil(construir, redis):  # noqa: F811
    servicio, _, _ = construir()
    await servicio.obtener_perfil("c1")
    clave = "perfil:c1:open_finance"
    guardado = json.loads(await redis.get(clave))
    guardado["__obtenido_en"] = "no-es-una-fecha"
    await redis.set(clave, json.dumps(guardado))

    perfil = normalizar(await servicio.obtener_perfil("c1"))

    assert perfil.campos["ratio_endeudamiento"].obtenido_en is None
    assert perfil.campos["ratio_endeudamiento"].valor == 0.32
