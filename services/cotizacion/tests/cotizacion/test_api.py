import logging
from datetime import UTC, datetime

import pytest

from app.cotizacion.dominio import PerfilUnificado

VIDA = {
    "cliente_id": "cli-1",
    "producto": "microseguro_vida",
    "datos": {"edad": 35, "suma_asegurada": 50_000_000},
}
DISP = {
    "cliente_id": "cli-1",
    "producto": "proteccion_dispositivos",
    "datos": {
        "tipo_dispositivo": "celular",
        "valor_dispositivo": 2_500_000,
        "antiguedad_meses": 8,
    },
}
AHORA = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def test_cotiza_y_devuelve_prima_coberturas_vigencia_y_version_de_reglas(entorno):
    e = entorno(reloj=lambda: AHORA)

    r = e.client.post("/v1/cotizaciones", json=VIDA)

    assert r.status_code == 201
    c = r.json()
    assert (c["prima_anual"], c["prima_mensual"], c["moneda"], c["estado"]) == (
        175_000,
        14_583,
        "COP",
        "EMITIDA",
    )
    assert c["coberturas"] == [
        {"tipo": "muerte", "monto": 50_000_000, "deducible": 0},
        {"tipo": "incapacidad_total", "monto": 25_000_000, "deducible": 0},
    ]
    assert c["vigente_hasta"].startswith("2026-11-11T15:00:00")
    assert c["version_reglas"] == "2026.10.1-prototipo"
    assert (c["degradado"], c["aproximada"], c["aviso"]) == (False, False, None)
    assert sum(x["monto"] for x in c["componentes"]) == c["prima_anual"]


def test_el_segundo_producto_se_cotiza_por_la_misma_ruta(entorno):
    r = entorno().client.post("/v1/cotizaciones", json=DISP)

    assert r.status_code == 201
    assert r.json()["prima_anual"] == 135_844


def test_perfil_degradado_marca_la_cotizacion_y_aplica_recargo(entorno):
    e = entorno(PerfilUnificado("x", degradado=True))

    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    assert c["degradado"] is True
    assert c["prima_anual"] == 201_250
    assert e.filas("cotizaciones")[0]["degradado"] in (1, True)


def test_sin_consentimiento_es_aproximada_con_aviso(entorno):
    e = entorno(PerfilUnificado("x", con_consentimiento=False))

    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    assert c["aproximada"] is True
    assert c["degradado"] is False
    assert "puede cambiar al momento de la compra" in c["aviso"]
    assert c["prima_anual"] == 175_000


def test_la_misma_entrada_da_la_misma_prima_con_ids_distintos(entorno):
    e = entorno()

    a = e.client.post("/v1/cotizaciones", json=VIDA).json()
    b = e.client.post("/v1/cotizaciones", json=VIDA).json()

    assert a["prima_anual"] == b["prima_anual"]
    assert a["id"] != b["id"]


# ---- validación (criterio 6): 422 con el campo, sin calcular ni persistir ----


def test_producto_inexistente_no_calcula_ni_consulta_el_perfil(entorno):
    e = entorno()

    r = e.client.post("/v1/cotizaciones", json={**VIDA, "producto": "seguro_de_barcos"})

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["campo"] == "producto"
    assert error["trace_id"]
    assert e.perfiles.llamadas == 0
    assert e.filas("cotizaciones") == []


@pytest.mark.parametrize(
    ("datos", "campo"),
    [
        ({"edad": 17, "suma_asegurada": 50_000_000}, "datos.edad"),
        ({"edad": 35}, "datos.suma_asegurada"),
        ({"edad": "treinta", "suma_asegurada": 50_000_000}, "datos.edad"),
        ({"edad": 35, "suma_asegurada": 1}, "datos.suma_asegurada"),
        ({}, "datos.edad"),
    ],
)
def test_datos_invalidos_indican_el_campo(entorno, datos, campo):
    e = entorno()

    r = e.client.post("/v1/cotizaciones", json={**VIDA, "datos": datos})

    assert r.status_code == 422
    assert campo in [d["campo"] for d in r.json()["error"]["details"]]
    assert e.filas("cotizaciones") == []


@pytest.mark.parametrize(
    ("cuerpo", "campo"),
    [
        ({"producto": "microseguro_vida", "datos": {}}, "cliente_id"),
        ({"cliente_id": "c", "datos": {}}, "producto"),
        ({"cliente_id": "c", "producto": "microseguro_vida"}, "datos"),
        ({**VIDA, "cliente_id": "x" * 37}, "cliente_id"),
        ({**VIDA, "datos": "no es objeto"}, "datos"),
    ],
)
def test_errores_de_forma_usan_el_formato_estandar(entorno, cuerpo, campo):
    r = entorno().client.post("/v1/cotizaciones", json=cuerpo)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert campo in [d["campo"] for d in r.json()["error"]["details"]]


# ---- persistencia (criterio 7) y registro de latencia (criterio 9) ----


def test_persiste_cotizacion_coberturas_y_log_de_latencia(entorno):
    tics = iter([100.0, 100.0425])  # inicio y fin: 42,5 ms
    e = entorno(cronometro=lambda: next(tics))

    c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    (cot,) = e.filas("cotizaciones")
    assert (cot["id"], cot["cliente_id"], cot["producto"], cot["version_reglas"]) == (
        c["id"],
        "cli-1",
        "microseguro_vida",
        "2026.10.1-prototipo",
    )
    assert float(cot["prima_anual"]) == 175_000
    assert [(f["tipo"], float(f["monto"])) for f in e.filas("coberturas")] == [
        ("muerte", 50_000_000.0),
        ("incapacidad_total", 25_000_000.0),
    ]
    (log,) = e.filas("log_latencia")
    assert log["cotizacion_id"] == c["id"]
    assert log["latencia_interna_ms"] == pytest.approx(42.5)
    assert log["latencia_cliente_ms"] is None
    assert (log["hit_cache"], log["degradado"]) in [(1, 0), (True, False)]
    assert c["metadata"] == {"hit_cache": True, "latencia_interna_ms": 42.5}


def test_el_cliente_reporta_su_latencia_y_se_completa_el_log(entorno):
    e = entorno()
    cid = e.client.post("/v1/cotizaciones", json=VIDA).json()["id"]

    r = e.client.put(
        f"/v1/cotizaciones/{cid}/latencia-cliente", json={"latencia_cliente_ms": 231.5}
    )

    assert r.status_code == 204
    assert e.filas("log_latencia")[0]["latencia_cliente_ms"] == 231.5


@pytest.mark.parametrize("valor", [0, -5, "rápido"])
def test_latencia_cliente_invalida(entorno, valor):
    e = entorno()
    cid = e.client.post("/v1/cotizaciones", json=VIDA).json()["id"]

    r = e.client.put(
        f"/v1/cotizaciones/{cid}/latencia-cliente", json={"latencia_cliente_ms": valor}
    )

    assert r.status_code == 422


def test_latencia_de_cotizacion_inexistente_es_404(entorno):
    r = entorno().client.put(
        "/v1/cotizaciones/no-existe/latencia-cliente", json={"latencia_cliente_ms": 10}
    )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "COTIZACION_NO_ENCONTRADA"


def test_si_falla_el_log_de_latencia_la_cotizacion_se_emite_igual(entorno, monkeypatch):
    e = entorno()

    def roto(*_a, **_k):
        raise RuntimeError("base caída")

    monkeypatch.setattr(e.repositorio, "guardar_latencia", roto)

    r = e.client.post("/v1/cotizaciones", json=VIDA)

    assert r.status_code == 201
    assert len(e.filas("cotizaciones")) == 1


def test_si_falla_guardar_la_cotizacion_no_queda_nada_a_medias(entorno, monkeypatch):
    e = entorno()
    monkeypatch.setattr(
        e.repositorio,
        "guardar",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
    )

    with pytest.raises(RuntimeError):
        e.client.post("/v1/cotizaciones", json=VIDA)

    assert e.filas("cotizaciones") == [] and e.filas("log_latencia") == []


# ---- consulta ----


def test_consulta_una_cotizacion_emitida(entorno):
    e = entorno()
    creada = e.client.post("/v1/cotizaciones", json=DISP).json()

    r = e.client.get(f"/v1/cotizaciones/{creada['id']}")

    assert r.status_code == 200
    c = r.json()
    assert sum(x["monto"] for x in c["componentes"]) == c["prima_anual"]
    assert c["componentes"] == creada["componentes"]  # sale del desglose persistido
    for campo in (
        "id",
        "prima_anual",
        "prima_mensual",
        "coberturas",
        "version_reglas",
        "vigente_hasta",
        "aproximada",
    ):
        assert c[campo] == creada[campo]
    assert c["metadata"]["hit_cache"] is True


def test_consulta_inexistente_es_404_con_formato_estandar(entorno):
    r = entorno().client.get("/v1/cotizaciones/no-existe")

    assert r.status_code == 404
    assert set(r.json()["error"]) == {"code", "message", "details", "trace_id"}


def test_id_demasiado_largo_es_422(entorno):
    assert entorno().client.get("/v1/cotizaciones/" + "x" * 40).status_code == 422


def test_al_emitir_deja_un_log_con_el_id_el_producto_y_la_prima(entorno, caplog):
    e = entorno(reloj=lambda: AHORA)

    with caplog.at_level(logging.INFO, logger="cotizacion.emision"):
        c = e.client.post("/v1/cotizaciones", json=VIDA).json()

    mensajes = [
        r.getMessage() for r in caplog.records if r.name == "cotizacion.emision"
    ]
    assert mensajes == [
        f"cotizacion_emitida id={c['id']} producto=microseguro_vida prima_anual=175000 degradado=False"
    ]


def test_el_log_de_emision_no_lleva_datos_personales_ni_de_la_solicitud(
    entorno, caplog
):
    e = entorno(reloj=lambda: AHORA)

    with caplog.at_level(logging.DEBUG):
        e.client.post("/v1/cotizaciones", json=VIDA)

    volcado = caplog.text
    # cliente_id, edad y suma asegurada son datos del cliente: no deben aparecer en ningún log.
    assert "cli-1" not in volcado
    assert "50000000" not in volcado
