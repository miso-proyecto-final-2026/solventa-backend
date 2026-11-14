"""HU24: linaje completo, reconstruible, marcado y auditado (quién lo consultó)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.proveedores.dominio import ProveedorError
from tests.perfilamiento.conftest import CLIENTE, ORIGEN

ASESOR = "asesor-7"


def _linaje(entorno, cliente_id=CLIENTE, perfil_id=None, consultado_por=None):
    cabeceras = dict(entorno.cabeceras)
    if consultado_por is not None:
        cabeceras["X-Consultado-Por"] = consultado_por
    params = {"perfil_id": perfil_id} if perfil_id else None
    return entorno.cliente.get(
        f"/v1/perfilamiento/{cliente_id}/linaje", headers=cabeceras, params=params
    )


def _por_fuente(cuerpo):
    return {f["fuente"]: f for f in cuerpo["fuentes"]}


# --- Criterio 1: completo -------------------------------------------------------------------


def test_cada_fuente_trae_tipo_de_dato_fecha_ttl_y_estado_del_consentimiento(entorno):
    entorno.enriquecer()

    cuerpo = _linaje(entorno).json()

    of, od = _por_fuente(cuerpo)["open_finance"], _por_fuente(cuerpo)["open_data"]
    assert of["tipo_dato"] == "FINANCIERO" and od["tipo_dato"] == "SOCIOECONOMICO"
    for linea in (of, od):
        assert linea["obtenido_en"].startswith("2026-10-21T12:00:00")
        assert linea["ttl_s"] in (3600, 86400)
        assert linea["estado_consentimiento"] == "OTORGADO"
    assert cuerpo["consentimiento_id"] == f"stub-{CLIENTE}"


# --- Criterio 2: reconstruible --------------------------------------------------------------


def test_con_el_linaje_se_reconstruye_de_donde_salio_cada_campo_del_perfil(entorno):
    entorno.open_data.error = ProveedorError("503")  # un perfil con huecos, para que haya ausentes
    perfil = entorno.enriquecer().json()

    linaje = _linaje(entorno).json()

    assert linaje["perfil_id"] == perfil["perfil_id"]
    origen_por_campo = {c: f["fuente"] for f in linaje["fuentes"] for c in f["campos"]}
    # Cada campo del perfil aparece en exactamente una fuente del linaje, la misma que el perfil dice.
    assert origen_por_campo.keys() == perfil["campos"].keys()
    for nombre, campo in perfil["campos"].items():
        assert origen_por_campo[nombre] == campo["fuente"]
        linea = _por_fuente(linaje)[campo["fuente"]]
        assert linea["obtenido_en"] == campo["obtenido_en"]
    # Lo que falta también se explica: mismo campo y mismo motivo que en el perfil.
    assert linaje["ausentes"] == perfil["ausentes"]
    assert linaje["ausentes"]["estrato"] == "sin_datos"
    assert linaje["conflictos"] == perfil["conflictos"]


def test_un_perfil_anterior_sigue_consultable_por_su_id(entorno):
    primero = entorno.enriquecer().json()["perfil_id"]
    entorno.open_finance.error = ProveedorError("503")
    entorno.cache.obtener = _sin_cache
    segundo = entorno.enriquecer().json()["perfil_id"]

    vigente = _linaje(entorno).json()
    anterior = _linaje(entorno, perfil_id=primero).json()

    assert vigente["perfil_id"] == segundo and anterior["perfil_id"] == primero
    assert _por_fuente(anterior)["open_finance"]["degradado"] is False
    assert _por_fuente(vigente)["open_finance"]["degradado"] is True


def test_el_perfil_de_otro_cliente_o_inexistente_responde_404(entorno):
    ajeno = entorno.enriquecer("cliente-2").json()["perfil_id"]
    entorno.enriquecer()

    assert _linaje(entorno, perfil_id=ajeno).status_code == 404
    assert _linaje(entorno, perfil_id="no-existe").status_code == 404


# --- Criterio 3: caché y valor por defecto marcados ------------------------------------------


def test_lo_servido_desde_cache_queda_marcado_con_la_fecha_de_la_consulta_original(entorno):
    entorno.enriquecer()
    entorno.enriquecer()  # sale de caché

    of = _por_fuente(_linaje(entorno).json())["open_finance"]

    assert of["hit_cache"] is True and of["por_defecto"] is False
    assert of["obtenido_en"].startswith("2026-10-21T12:00:00")


def test_una_fuente_caida_queda_marcada_como_por_defecto(entorno):
    entorno.open_finance.error = ProveedorError("503")
    entorno.enriquecer()

    of = _por_fuente(_linaje(entorno).json())["open_finance"]

    assert of["por_defecto"] is True and of["degradado"] is True and of["campos"] == []


# --- Criterio 5: sin datos personales de más --------------------------------------------------


def test_el_linaje_completo_no_repite_ingresos_ni_scores(entorno):
    entorno.open_data.error = ProveedorError("503")
    entorno.enriquecer()

    cuerpo = _linaje(entorno).json()
    cuerpo.pop("perfil_id")  # UUID aleatorio: podría contener las cifras por azar
    texto = str(cuerpo)

    for cifra in ("4500000", "4_500_000", "710", "0.32", "1440000"):
        assert cifra not in texto


# --- Criterio 6: quién consultó ---------------------------------------------------------------


def test_cada_consulta_registra_el_servicio_y_la_persona_a_cuyo_nombre_consulta(entorno):
    entorno.enriquecer()

    assert _linaje(entorno, consultado_por=ASESOR).status_code == 200

    (consulta,) = entorno.repo.consultas_linaje_de(CLIENTE)
    assert consulta.origen == ORIGEN
    assert consulta.consultado_por == ASESOR
    assert consulta.resultado == "ENTREGADO"
    assert consulta.perfil_id is not None
    assert consulta.consultado_en == datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_sin_cabecera_de_persona_se_registra_solo_el_servicio(entorno):
    entorno.enriquecer()

    _linaje(entorno)

    (consulta,) = entorno.repo.consultas_linaje_de(CLIENTE)
    assert consulta.origen == ORIGEN and consulta.consultado_por is None


def test_la_consulta_sin_perfil_tambien_queda_registrada(entorno):
    assert _linaje(entorno, consultado_por=ASESOR).status_code == 404

    (consulta,) = entorno.repo.consultas_linaje_de(CLIENTE)
    assert consulta.resultado == "SIN_PERFIL" and consulta.perfil_id is None


def test_una_persona_con_formato_invalido_responde_422_y_no_se_registra(entorno):
    entorno.enriquecer()

    r = _linaje(entorno, consultado_por="asesor 7; drop table")

    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["campo"] == "X-Consultado-Por"
    assert entorno.repo.consultas_linaje_de(CLIENTE) == []


def test_tras_revocar_el_linaje_se_sigue_entregando_y_registrando_hasta_que_decida_v12(entorno):
    # Provisional (V-12, #106): el linaje es evidencia de auditoría y no exige consentimiento.
    entorno.enriquecer()
    entorno.revocar()

    assert _linaje(entorno, consultado_por=ASESOR).status_code == 200
    assert len(entorno.repo.consultas_linaje_de(CLIENTE)) == 1


async def _sin_cache(*_args):
    return None
