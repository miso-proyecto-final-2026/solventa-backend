"""Decisión automática (HU15, criterios 2 a 5 y 8): una prueba por regla y por motivo, con casos de contorno."""

import ast
from pathlib import Path

import pytest
from app.suscripcion.decision import decidir
from app.suscripcion.dominio import Decision, PerfilRiesgo, SolicitudDecision
from app.suscripcion.reglas import Reglas, cargar_reglas

REGLAS = cargar_reglas("v1")


def perfil(*, ratio=0.30, reportes=0, completitud=1.0, **extra):
    campos = {"ratio_endeudamiento": ratio, "reportes_negativos": reportes, "ingresos_mensuales_cop": 4_500_000}
    campos.update(extra.pop("campos", {}))
    base = {"disponible": True, "con_consentimiento": True, "degradado": False, "completitud": completitud, "campos": campos}
    base.update(extra)
    return PerfilRiesgo(**base)


def solicitud(monto=30_000_000, **kwargs):
    return SolicitudDecision(producto="microseguro_vida", monto_asegurado=monto, perfil=perfil(**kwargs))


def decision(monto=30_000_000, **kwargs):
    return decidir(solicitud(monto, **kwargs), REGLAS)


# --- aprobar ---------------------------------------------------------------------------------------------------

def test_un_caso_estandar_se_aprueba_sin_ajuste():
    r = decision()

    assert r.decision is Decision.APROBADA and r.ajuste is None and r.reglas_version == REGLAS.version


@pytest.mark.parametrize(("monto", "ratio"), [(50_000_000, 0.50), (5_000_000, 0.0), (50_000_000, 0.4999)])
def test_los_limites_de_aprobacion_se_incluyen(monto, ratio):
    assert decision(monto, ratio=ratio).decision is Decision.APROBADA


# --- ajustar ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ratio", [0.5001, 0.60, 0.65])
def test_un_endeudamiento_intermedio_se_aprueba_ajustado(ratio):
    r = decision(30_000_000, ratio=ratio)

    assert r.decision is Decision.AJUSTADA
    assert r.ajuste == {"monto_original": 30_000_000, "monto_asegurado": 30_000_000}  # no sube el monto
    assert any("endeudamiento" in m.lower() for m in r.motivos)


@pytest.mark.parametrize("monto", [50_000_001, 65_000_000, 80_000_000])
def test_un_monto_intermedio_se_ajusta_al_limite_de_aprobacion(monto):
    r = decision(monto)

    assert r.decision is Decision.AJUSTADA
    assert r.ajuste == {"monto_original": monto, "monto_asegurado": 50_000_000}
    assert any("monto" in m.lower() for m in r.motivos)


def test_si_se_cumplen_ambos_motivos_de_ajuste_se_listan_los_dos():
    r = decision(70_000_000, ratio=0.60)

    assert r.decision is Decision.AJUSTADA and len(r.motivos) == 2
    assert r.ajuste["monto_asegurado"] == 50_000_000


# --- rechazar --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ratio", [0.6501, 0.80, 1.0])
def test_un_endeudamiento_excesivo_se_rechaza_con_motivo_legible(ratio):
    r = decision(ratio=ratio)

    assert r.decision is Decision.RECHAZADA and r.ajuste is None
    assert r.motivos and all(isinstance(m, str) and len(m) > 15 for m in r.motivos)
    assert any("endeudamiento" in m.lower() for m in r.motivos)


def test_mas_de_dos_reportes_negativos_se_rechaza():
    r = decision(reportes=3)

    assert r.decision is Decision.RECHAZADA and any("reporte" in m.lower() for m in r.motivos)


def test_el_rechazo_tiene_prioridad_sobre_un_monto_alto():
    assert decision(90_000_000, ratio=0.70).decision is Decision.RECHAZADA


# --- derivar a revisión asistida -------------------------------------------------------------------------------

@pytest.mark.parametrize("reportes", [1, 2])
def test_uno_o_dos_reportes_negativos_salen_del_rango_estandar_y_se_derivan(reportes):
    r = decision(reportes=reportes)

    assert r.decision is Decision.DERIVADA and any("reporte" in m.lower() for m in r.motivos)


def test_un_monto_sobre_el_limite_automatico_se_deriva():
    r = decision(80_000_001)

    assert r.decision is Decision.DERIVADA and any("límite" in m.lower() or "limite" in m.lower() for m in r.motivos)


@pytest.mark.parametrize(
    ("kwargs", "palabra"),
    [
        ({"disponible": False}, "disponible"),
        ({"con_consentimiento": False}, "consentimiento"),
        ({"degradado": True}, "incompleto"),
        ({"completitud": 0.5}, "incompleto"),
    ],
)
def test_un_perfil_que_no_sirve_se_deriva_con_su_motivo(kwargs, palabra):
    r = decision(**kwargs)

    assert r.decision is Decision.DERIVADA and any(palabra in m.lower() for m in r.motivos)


@pytest.mark.parametrize("campo", ["ratio_endeudamiento", "reportes_negativos"])
def test_un_campo_requerido_ausente_se_deriva(campo):
    campos = {"ratio_endeudamiento": 0.3, "reportes_negativos": 0}
    del campos[campo]
    s = SolicitudDecision(
        producto="microseguro_vida",
        monto_asegurado=30_000_000,
        perfil=PerfilRiesgo(disponible=True, con_consentimiento=True, degradado=False, completitud=1.0, campos=campos),
    )

    r = decidir(s, REGLAS)

    assert r.decision is Decision.DERIVADA and any(campo in m for m in r.motivos)


def test_los_datos_incompletos_se_derivan_antes_que_rechazar():
    """Sin datos confiables no se rechaza a nadie: lo decide una persona."""
    r = decision(ratio=0.9, completitud=0.3)

    assert r.decision is Decision.DERIVADA


# --- propiedades generales -------------------------------------------------------------------------------------

def test_la_misma_entrada_da_siempre_la_misma_salida():
    assert decision(70_000_000, ratio=0.6) == decision(70_000_000, ratio=0.6)


def test_toda_decision_lleva_motivos_salvo_la_aprobacion_limpia():
    assert decision().motivos == []
    for r in (decision(ratio=0.7), decision(reportes=1), decision(60_000_000)):
        assert r.motivos


# --- reglas versionadas y aisladas (EC-M1.2) --------------------------------------------------------------------

def test_las_reglas_cargadas_son_provisionales_y_traen_su_version():
    assert REGLAS.provisional is True and REGLAS.version.startswith("2026.10.1")


def test_unas_reglas_incoherentes_no_se_cargan():
    with pytest.raises(ValueError):
        Reglas(**{**vars(REGLAS), "endeudamiento_aprobacion": 0.9})  # mayor que el máximo con ajuste
    with pytest.raises(ValueError):
        Reglas(**{**vars(REGLAS), "monto_ajustado_a": 90_000_000})  # mayor que el máximo automático


def test_una_version_de_reglas_inexistente_falla_con_un_mensaje_claro():
    with pytest.raises(FileNotFoundError, match="v99"):
        cargar_reglas("v99")


def test_los_umbrales_se_leen_del_archivo_y_no_estan_en_el_codigo():
    """Cambiar un umbral toca un solo archivo: el código de decisión no trae los valores."""
    codigo = Path("app/suscripcion/decision.py").read_text(encoding="utf-8")

    for numero in ("50000000", "50_000_000", "80_000_000", "0.65", "0.50"):
        assert numero not in codigo


def test_las_reglas_no_dependen_de_otros_modulos_del_servicio():
    """Aislamiento: ni el motor de rating ni el cobro ni la API se importan desde las reglas."""
    for archivo in ("reglas.py", "decision.py"):
        arbol = ast.parse(Path("app/suscripcion", archivo).read_text(encoding="utf-8"))
        importados = {n.module or "" for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}
        importados |= {a.name for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
        ajenos = {m for m in importados if m.startswith("app.") and m not in {"app.suscripcion.dominio", "app.suscripcion.reglas"}}
        assert not ajenos, f"{archivo} importa {ajenos}"
