"""Pruebas del desglose del precio persistido y su consulta de auditoría (HU14)."""

import inspect as inspeccion
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import app.cotizacion.servicio as modulo_servicio
from app.cotizacion.desglose import (
    a_json,
    desde_json,
    recalcular,
    verificar_invariante,
)
from app.cotizacion.dominio import (
    ETAPA_DATO,
    ETAPA_RIESGO_BASE,
    ETAPA_SENAL,
    PerfilUnificado,
)
from app.cotizacion.errores import ErrorDesglose
from app.cotizacion.motor import calcular_prima
from app.cotizacion.repositorio import RepositorioCotizaciones
from tests.cotizacion.test_api import DISP, VIDA
from tests.cotizacion.test_motor_propiedades import (
    NOMBRES,
    REGLAS,
    datos_validos,
    senales,
)

CLAVE = "clave-auditor"
OTRA = "clave-de-otro-servicio"
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

PERFIL = PerfilUnificado(
    "x",
    senales={"ratio_endeudamiento": 0.32, "reportes_negativos": 0},
    fuentes=("open_finance", "open_data"),
    completitud=1.0,
    procedencia={
        "ratio_endeudamiento": ("open_finance", T0),
        "reportes_negativos": ("open_data", T0),
    },
)


def _auditor(e, *claves):
    e.client.app.state.auditor_api_keys = claves or (CLAVE,)
    return {"X-Service-Key": CLAVE}


def _desglose(producto="proteccion_dispositivos", datos=None, perfil=PERFIL):
    datos = datos or DISP["datos"]
    return calcular_prima(producto, datos, perfil, REGLAS).desglose


# --- criterio 1: componentes registrados --------------------------------------


def test_el_desglose_trae_base_factores_recargos_y_version_de_reglas():
    d = _desglose()

    assert d.version_reglas == REGLAS.version
    assert d.prima_base == Decimal(112500)
    por_etapa = {(f.etapa, f.nombre): f.factor for f in d.factores}
    assert por_etapa[(ETAPA_DATO, "tipo_dispositivo")] == Decimal("1.15")
    assert por_etapa[(ETAPA_RIESGO_BASE, "factor_riesgo_base")] == Decimal("1.05")
    assert por_etapa[(ETAPA_SENAL, "ratio_endeudamiento")] == Decimal("1.00")
    assert [c.concepto for c in d.componentes] == [
        "prima_base",
        "ajuste_datos",
        "ajuste_riesgo",
        "recargo_degradado",
    ]
    assert d.recargo_degradado_pct is None


def test_en_degradado_el_desglose_lleva_el_recargo_y_no_las_senales():
    d = _desglose(perfil=replace(PERFIL, degradado=True))

    assert d.recargo_degradado_pct == Decimal("0.15")
    assert d.perfil.degradado is True
    assert not [f for f in d.factores if f.etapa == ETAPA_SENAL]


def test_sin_consentimiento_el_desglose_no_usa_senales():
    d = _desglose(perfil=PerfilUnificado("x", con_consentimiento=False))

    assert d.perfil.con_consentimiento is False
    assert not [f for f in d.factores if f.etapa == ETAPA_SENAL]


# --- criterio 3: origen de los datos, sin datos personales ----------------------


def test_cada_senal_cita_su_fuente_y_fecha():
    senal = {f.nombre: f for f in _desglose().factores if f.etapa == ETAPA_SENAL}

    assert (
        senal["ratio_endeudamiento"].fuente,
        senal["ratio_endeudamiento"].obtenido_en,
    ) == (
        "open_finance",
        T0,
    )
    assert senal["reportes_negativos"].fuente == "open_data"


def test_el_desglose_no_contiene_datos_personales_ni_valores_del_cliente():
    perfil = replace(
        PERFIL,
        cliente_id="cli-secreto-123",
        senales={"ratio_endeudamiento": 0.3217, "reportes_negativos": 0},
    )
    d = calcular_prima(
        "proteccion_dispositivos",
        {**DISP["datos"], "valor_dispositivo": 2_731_904},
        perfil,
        REGLAS,
    ).desglose

    texto = json.dumps(a_json(d))

    assert "cli-secreto-123" not in texto and "cliente" not in texto
    assert "0.3217" not in texto  # el valor de la señal no se copia, solo su factor
    assert "2731904" not in texto  # tampoco los datos del producto


# --- criterio 2: invariante ---------------------------------------------------


def test_la_suma_de_componentes_es_la_prima_final():
    d = _desglose()

    assert sum(c.monto for c in d.componentes) == d.prima_anual
    verificar_invariante(d)


def test_un_desglose_cuya_suma_no_coincide_se_rechaza():
    d = _desglose()
    roto = replace(d, prima_anual=d.prima_anual + 1)

    with pytest.raises(ErrorDesglose, match="no coincide"):
        verificar_invariante(roto)


def test_un_desglose_que_no_reproduce_la_prima_se_rechaza():
    d = _desglose()
    # la suma sigue cuadrando, pero la base ya no explica el precio
    roto = replace(d, prima_base=d.prima_base + 10)

    with pytest.raises(ErrorDesglose, match="no reproduce"):
        verificar_invariante(roto)


def test_si_la_invariante_falla_no_se_emite_ni_se_persiste_y_se_registra(
    entorno, monkeypatch, caplog
):
    e = entorno()
    original = modulo_servicio.calcular_prima

    def con_error(*args):
        r = original(*args)
        return replace(r, desglose=replace(r.desglose, prima_anual=r.prima_anual + 5))

    monkeypatch.setattr(modulo_servicio, "calcular_prima", con_error)
    caplog.set_level(logging.ERROR)

    r = e.client.post("/v1/cotizaciones", json=VIDA)

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "DESGLOSE_INCONSISTENTE"
    assert "desglose inconsistente" in caplog.text
    assert e.filas("cotizaciones") == [] and e.filas("desgloses_precio") == []


# --- criterio 6: reproducibilidad ---------------------------------------------


@given(st.data())
def test_recalcular_desde_el_desglose_persistido_da_el_mismo_precio(data):
    producto = data.draw(st.sampled_from(NOMBRES))
    datos = data.draw(datos_validos(producto))
    perfil = PerfilUnificado(
        "x",
        senales=data.draw(senales(producto)),
        degradado=data.draw(st.booleans()),
        con_consentimiento=data.draw(st.booleans()),
    )

    resultado = calcular_prima(producto, datos, perfil, REGLAS)
    guardado = desde_json(json.loads(json.dumps(a_json(resultado.desglose))))

    assert guardado == resultado.desglose
    assert recalcular(guardado) == resultado.prima_anual
    assert sum(c.monto for c in guardado.componentes) == resultado.prima_anual


def test_el_ida_y_vuelta_json_conserva_exactitud_decimal_y_fechas():
    d = _desglose()

    assert desde_json(json.loads(json.dumps(a_json(d)))) == d
    assert a_json(d)["factores"][-1]["obtenido_en"] == T0.isoformat()


def test_una_version_de_esquema_desconocida_no_se_interpreta():
    datos = a_json(_desglose()) | {"version_esquema": 99}

    with pytest.raises(ErrorDesglose, match="no soportada"):
        desde_json(datos)


# --- persistencia y criterio 5: inmutabilidad -----------------------------------


def test_la_cotizacion_y_su_desglose_se_guardan_juntos(entorno):
    e = entorno(PERFIL)

    c = e.client.post("/v1/cotizaciones", json=DISP).json()

    filas = e.filas("desgloses_precio")
    assert [f["cotizacion_id"] for f in filas] == [c["id"]]
    assert filas[0]["version_esquema"] == 1
    assert (
        desde_json(
            filas[0]["detalle"]
            if isinstance(filas[0]["detalle"], dict)
            else json.loads(filas[0]["detalle"])
        ).prima_anual
        == c["prima_anual"]
    )


def test_si_falla_el_desglose_no_queda_la_cotizacion_a_medias(entorno, monkeypatch):
    e = entorno()
    import app.cotizacion.repositorio as repo

    def falla(_):
        raise RuntimeError("no serializable")

    monkeypatch.setattr(repo, "a_json", falla)

    with pytest.raises(RuntimeError):
        e.client.post("/v1/cotizaciones", json=VIDA)

    assert e.filas("cotizaciones") == [] and e.filas("coberturas") == []


def test_el_repositorio_no_ofrece_ninguna_operacion_para_modificar_un_desglose():
    metodos = {
        n
        for n, _ in inspeccion.getmembers(
            RepositorioCotizaciones, inspeccion.isfunction
        )
        if "desglose" in n
    }

    assert metodos == {"obtener_desglose"}  # solo lectura; se inserta con `guardar`


def test_recalcular_genera_una_cotizacion_nueva_y_no_toca_la_anterior(entorno):
    e = entorno(PERFIL)
    h = _auditor(e)

    primera = e.client.post("/v1/cotizaciones", json=DISP).json()
    antes = e.client.get(f"/v1/cotizaciones/{primera['id']}/desglose", headers=h).json()
    segunda = e.client.post("/v1/cotizaciones", json=DISP).json()
    despues = e.client.get(
        f"/v1/cotizaciones/{primera['id']}/desglose", headers=h
    ).json()

    assert segunda["id"] != primera["id"]
    assert antes == despues
    assert len(e.filas("desgloses_precio")) == 2


# --- criterio 4: consulta y control de acceso ---------------------------------


def test_el_auditor_obtiene_el_desglose_completo(entorno):
    e = entorno(PERFIL)
    h = _auditor(e)
    c = e.client.post("/v1/cotizaciones", json=DISP).json()

    r = e.client.get(f"/v1/cotizaciones/{c['id']}/desglose", headers=h)

    assert r.status_code == 200
    d = r.json()
    assert d["cotizacion_id"] == c["id"]
    assert d["prima_anual"] == c["prima_anual"]
    assert sum(x["monto"] for x in d["componentes"]) == d["prima_anual"]
    assert d["version_reglas"] == c["version_reglas"]
    assert d["perfil"]["fuentes"] == ["open_finance", "open_data"]
    assert {f["etapa"] for f in d["factores"]} == {"dato", "riesgo_base", "senal"}


def test_sin_credencial_se_deniega_con_401(entorno):
    e = entorno()
    _auditor(e)
    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    r = e.client.get(f"/v1/cotizaciones/{c['id']}/desglose")

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NO_AUTENTICADO"


def test_una_clave_sin_permiso_se_deniega_con_403_y_no_revela_datos(entorno):
    e = entorno()
    _auditor(e)
    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    r = e.client.get(
        f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": OTRA}
    )

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "SIN_PERMISO"
    assert "prima" not in json.dumps(r.json())


def test_sin_claves_configuradas_nadie_accede(entorno):
    e = entorno()
    e.client.app.state.auditor_api_keys = ()
    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    r = e.client.get(
        f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": CLAVE}
    )

    assert r.status_code == 403


def test_la_clave_vacia_nunca_coincide(entorno):
    e = entorno()
    e.client.app.state.auditor_api_keys = ("",)
    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    r = e.client.get(
        f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": ""}
    )

    assert r.status_code == 401


def test_el_acceso_se_valida_antes_de_buscar_la_cotizacion(entorno):
    e = entorno()
    _auditor(e)

    sin_clave = e.client.get("/v1/cotizaciones/no-existe/desglose")

    assert sin_clave.status_code == 401  # no se revela si la cotización existe


def test_cotizacion_inexistente_con_credencial_valida_da_404(entorno):
    e = entorno()
    h = _auditor(e)

    r = e.client.get("/v1/cotizaciones/no-existe/desglose", headers=h)

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "COTIZACION_NO_ENCONTRADA"


def test_cotizacion_anterior_a_hu14_responde_sin_desglose(entorno):
    e = entorno()
    h = _auditor(e)
    c = e.client.post("/v1/cotizaciones", json=VIDA).json()
    with e.engine.begin() as conexion:
        conexion.exec_driver_sql("DELETE FROM desgloses_precio")

    r = e.client.get(f"/v1/cotizaciones/{c['id']}/desglose", headers=h)
    consulta = e.client.get(f"/v1/cotizaciones/{c['id']}")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DESGLOSE_NO_DISPONIBLE"
    assert consulta.status_code == 200 and consulta.json()["componentes"] is None


def test_la_cotizacion_consultada_trae_los_componentes_persistidos(entorno):
    e = entorno(PERFIL)
    c = e.client.post("/v1/cotizaciones", json=DISP).json()

    consulta = e.client.get(f"/v1/cotizaciones/{c['id']}").json()

    assert consulta["componentes"] == c["componentes"]
