"""HU16: reglas puras de la revisión asistida: categorías, prioridad y ajuste de la cobertura."""

from dataclasses import replace
from datetime import timedelta

import pytest
from app.suscripcion.api_revision import DecisionRevisionIn
from app.suscripcion.decision import decidir
from app.suscripcion.dominio import Decision, PerfilRiesgo, SolicitudDecision
from app.suscripcion.errores import AjusteInvalido
from app.suscripcion.reglas import cargar_reglas
from app.suscripcion.revision import (
    CategoriaMotivo,
    MotivoSinCategoria,
    Prioridad,
    UmbralesPrioridad,
    ajustar,
    categorizar,
)
from app.suscripcion.revision import prioridad as calcular_prioridad
from pydantic import ValidationError

from tests.dobles import AHORA, PERFIL_ESTANDAR

REGLAS = cargar_reglas()


def _derivar(perfil: PerfilRiesgo, monto: int = 40_000_000) -> list[str]:
    resultado = decidir(SolicitudDecision(producto="microseguro_vida", monto_asegurado=monto, perfil=perfil), REGLAS)
    assert resultado.decision is Decision.DERIVADA
    return resultado.motivos


# --- categorizar: los mensajes reales de decision.py -----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("perfil", "monto", "esperada"),
    [
        (PerfilRiesgo.no_disponible("Perfilamiento no responde."), 40_000_000, CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES),
        (PerfilRiesgo.sin_consentimiento(), 40_000_000, CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES),
        (replace(PERFIL_ESTANDAR, completitud=0.5), 40_000_000, CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES),
        (replace(PERFIL_ESTANDAR, campos={"ratio_endeudamiento": 0.3}), 40_000_000, CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES),
        (PERFIL_ESTANDAR, 90_000_000, CategoriaMotivo.MONTO_SOBRE_LIMITE),
        (
            replace(PERFIL_ESTANDAR, campos={"ratio_endeudamiento": 0.3, "reportes_negativos": 1}),
            40_000_000,
            CategoriaMotivo.PERFIL_FUERA_DE_RANGO,
        ),
    ],
)
def test_todos_los_motivos_reales_de_la_decision_tienen_categoria(perfil, monto, esperada):
    motivos = _derivar(perfil, monto)
    assert motivos
    # Estricto: un motivo nuevo en decision.py sin categoría hace fallar esta prueba.
    assert {categorizar(m, estricto=True) for m in motivos} == {esperada}


def test_un_motivo_desconocido_cae_en_datos_salvo_en_modo_estricto():
    assert categorizar("algo nuevo") is CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES
    with pytest.raises(MotivoSinCategoria):
        categorizar("algo nuevo", estricto=True)


# --- prioridad ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("horas", "minutos", "esperada"),
    [
        (0, 0, Prioridad.BAJA),
        (3, 59, Prioridad.BAJA),
        (4, 0, Prioridad.MEDIA),
        (23, 59, Prioridad.MEDIA),
        (24, 0, Prioridad.ALTA),
        (200, 0, Prioridad.ALTA),
    ],
)
def test_prioridad_por_antiguedad_en_los_limites(horas, minutos, esperada):
    creada = AHORA - timedelta(hours=horas, minutes=minutos)
    assert calcular_prioridad(creada, AHORA, UmbralesPrioridad(24, 4)) is esperada


def test_los_umbrales_son_configurables():
    assert calcular_prioridad(AHORA - timedelta(hours=2), AHORA, UmbralesPrioridad(alta_horas=2, media_horas=1)) is Prioridad.ALTA


# --- ajustar --------------------------------------------------------------------------------------------------------

COBERTURAS = [{"tipo": "muerte", "monto": 40_000_000, "deducible": 0}, {"tipo": "incapacidad_total", "monto": 20_000_000, "deducible": 500_000}]


def _ajustar(nuevo: int, **kw):
    datos = {"monto_asegurado": 40_000_000, "prima_mensual": 14_583, "prima_anual": 175_000, "coberturas": COBERTURAS}
    return ajustar(**{**datos, "nuevo_monto": nuevo, **kw})


def test_ajustar_escala_coberturas_y_primas_en_proporcion():
    a = _ajustar(30_000_000)

    assert a.factor == 0.75
    assert (a.monto_asegurado, a.monto_original) == (30_000_000, 40_000_000)
    assert (a.prima_anual, a.prima_mensual) == (131_250, 10_937)  # 14_583 * 0.75 = 10_937.25
    assert [c["monto"] for c in a.coberturas] == [30_000_000, 15_000_000]
    assert [c["deducible"] for c in a.coberturas] == [0, 500_000]  # el deducible no cambia
    assert (a.prima_anual_original, a.prima_mensual_original) == (175_000, 14_583)


def test_ajustar_redondea_a_pesos_enteros():
    a = _ajustar(10_000_001)
    assert isinstance(a.prima_anual, int)
    assert isinstance(a.prima_mensual, int)


def test_ajustar_sin_coberturas_guardadas():
    assert _ajustar(20_000_000, coberturas=None).coberturas == []


@pytest.mark.parametrize("nuevo", [0, -5, 40_000_000, 40_000_001])
def test_ajustar_exige_un_monto_positivo_y_menor_al_actual(nuevo):
    with pytest.raises(AjusteInvalido):
        _ajustar(nuevo)


# --- el cuerpo de la decisión -----------------------------------------------------------------------------------------

JUSTIFICACION = "El cliente presentó soporte de ingresos."


def test_la_justificacion_se_recorta_antes_de_validar():
    assert DecisionRevisionIn(decision="APROBADA", justificacion=f"   {JUSTIFICACION}  ").justificacion == JUSTIFICACION
    with pytest.raises(ValidationError):
        DecisionRevisionIn(decision="APROBADA", justificacion=" " * 30)


@pytest.mark.parametrize("justificacion", ["", "corta", "a" * 19, "a" * 2001])
def test_la_justificacion_es_obligatoria_y_acotada(justificacion):
    with pytest.raises(ValidationError):
        DecisionRevisionIn(decision="APROBADA", justificacion=justificacion)


def test_el_monto_es_obligatorio_al_ajustar_y_prohibido_en_los_otros_casos():
    with pytest.raises(ValidationError):
        DecisionRevisionIn(decision="AJUSTADA", justificacion=JUSTIFICACION)
    for otra in ("APROBADA", "RECHAZADA"):
        with pytest.raises(ValidationError):
            DecisionRevisionIn(decision=otra, justificacion=JUSTIFICACION, monto_asegurado=1_000)
    assert DecisionRevisionIn(decision="AJUSTADA", justificacion=JUSTIFICACION, monto_asegurado=1_000).monto_asegurado == 1_000


def test_una_decision_desconocida_se_rechaza():
    with pytest.raises(ValidationError):
        DecisionRevisionIn(decision="DERIVADA", justificacion=JUSTIFICACION)
