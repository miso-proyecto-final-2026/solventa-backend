"""HU25: GET /v1/perfilamiento/{cliente_id}/oferta — precio individualizado con el perfil enriquecido."""

from __future__ import annotations

import statistics
import time

from tests.perfilamiento.conftest import CLAVE, CLIENTE, ORIGEN

OTRO = "cliente-2"


# --- Criterio 1: oferta por perfil ------------------------------------------------------------------------


def test_devuelve_la_oferta_con_la_prima_calculada_con_el_perfil(entorno):
    entorno.enriquecer()

    r = entorno.oferta()

    assert r.status_code == 200
    o = r.json()
    assert (o["producto"], o["moneda"], o["etapa_de_vida"]) == ("vida_hipotecario", "COP", "desarrollo")
    assert (o["suma_asegurada"], o["prima_anual"], o["prima_mensual"]) == (270_000_000, 486_000, 40_500)
    assert [c["tipo"] for c in o["coberturas"]] == ["muerte", "incapacidad_total_permanente"]
    assert isinstance(o["prima_anual"], int)


def test_la_suma_asegurada_puede_venir_en_la_consulta(entorno):
    entorno.enriquecer()

    o = entorno.oferta(suma=100_000_000).json()

    assert o["suma_asegurada"] == 100_000_000
    assert o["prima_anual"] == 180_000  # 0,0018 × 100 M; ningún factor del perfil de prueba mueve la base


def test_la_misma_consulta_da_siempre_el_mismo_precio(entorno):
    entorno.enriquecer()

    a, b = entorno.oferta().json(), entorno.oferta().json()

    assert {k: a[k] for k in ("prima_anual", "prima_mensual", "desglose", "version_reglas")} == {
        k: b[k] for k in ("prima_anual", "prima_mensual", "desglose", "version_reglas")
    }


# --- Criterio 2: individualizado ---------------------------------------------------------------------------


def test_dos_clientes_con_perfiles_distintos_reciben_precios_distintos(entorno):
    entorno.enriquecer()
    entorno.open_finance.payload = {**entorno.open_finance.payload, "debtRatio": 0.62, "creditScore": 420}
    entorno.enriquecer(OTRO)

    assert entorno.oferta(OTRO, suma=200_000_000).json()["prima_anual"] > entorno.oferta(CLIENTE, suma=200_000_000).json()["prima_anual"]


# --- Criterios 4 y 5: versión de reglas, desglose y linaje -------------------------------------------------


def test_la_respuesta_indica_la_version_de_las_reglas_y_el_desglose(entorno):
    entorno.enriquecer()

    o = entorno.oferta().json()

    assert o["version_reglas"] == "2026.11.1-prototipo"
    pasos = o["desglose"]
    assert pasos[0]["paso"] == "prima_base"
    assert sum(p["ajuste"] for p in pasos) == o["prima_anual"]
    assert {p["nombre"] for p in pasos if p["paso"] == "senal"} == {
        "ratio_endeudamiento", "reportes_negativos", "ingresos_mensuales_cop", "score_crediticio",
    }


def test_se_guarda_la_oferta_con_el_desglose_y_el_perfil_usado(entorno):
    perfil_id = entorno.enriquecer().json()["perfil_id"]

    o = entorno.oferta().json()
    (guardada,) = entorno.repo.ofertas_de(CLIENTE)

    assert (guardada.id, guardada.perfil_id, guardada.version_reglas) == (o["oferta_id"], perfil_id, "2026.11.1-prototipo")
    assert (guardada.prima_anual, guardada.suma_asegurada) == (o["prima_anual"], o["suma_asegurada"])
    assert guardada.desglose == o["desglose"]
    assert o["perfil_id"] == perfil_id
    # Por el perfil se llega al linaje de las fuentes que se usaron.
    assert entorno.linaje().json()["perfil_id"] == perfil_id


def test_cada_consulta_guarda_su_propia_oferta(entorno):
    entorno.enriquecer()

    entorno.oferta()
    entorno.oferta(suma=50_000_000)

    assert len(entorno.repo.ofertas_de(CLIENTE)) == 2


def test_la_oferta_usa_el_perfil_mas_reciente(entorno):
    entorno.enriquecer()
    entorno.open_finance.payload = {**entorno.open_finance.payload, "debtRatio": 0.62}
    nuevo = entorno.enriquecer().json()["perfil_id"]

    assert entorno.oferta().json()["perfil_id"] == nuevo


# --- Criterio 6: consentimiento ---------------------------------------------------------------------------


def test_sin_consentimiento_vigente_no_se_calcula_y_el_intento_queda_registrado(entorno):
    entorno.enriquecer()
    entorno.revocar()

    r = entorno.oferta()

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CONSENTIMIENTO_NO_VIGENTE"
    assert entorno.repo.ofertas_de(CLIENTE) == []
    (intento,) = entorno.repo.intentos_de(CLIENTE)
    assert (intento.operacion, intento.origen, intento.estado_consentimiento) == ("OFERTA", ORIGEN, "REVOCADO")


# --- Criterio 7: sin perfil --------------------------------------------------------------------------------


def test_sin_perfil_dice_que_no_hay_perfil_y_no_inventa_un_precio(entorno):
    r = entorno.oferta()

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PERFIL_NO_ENCONTRADO"
    assert entorno.repo.ofertas_de(CLIENTE) == []


# --- Validación y acceso -----------------------------------------------------------------------------------


def test_una_suma_fuera_de_los_limites_da_422_con_el_campo(entorno):
    entorno.enriquecer()

    r = entorno.oferta(suma=1_000)

    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["campo"] == "suma_asegurada"
    assert entorno.repo.ofertas_de(CLIENTE) == []


def test_una_suma_no_positiva_da_422(entorno):
    entorno.enriquecer()

    assert entorno.oferta(suma=0).status_code == 422


def test_un_identificador_invalido_da_422(entorno):
    assert entorno.oferta("a b;c").status_code == 422


def test_exige_la_clave_de_servicio(entorno):
    entorno.enriquecer()

    assert entorno.oferta(cabeceras={}).status_code == 401
    assert entorno.oferta(cabeceras={"X-Service-Key": "otra"}).status_code == 403
    assert entorno.oferta(cabeceras={"X-Service-Key": CLAVE}).status_code == 200


def test_la_oferta_no_expone_valores_del_proveedor_ni_identificadores(entorno):
    entorno.enriquecer()

    texto = entorno.oferta().text

    assert "customerId" not in texto and "documento_ref" not in texto and "open_finance" not in texto


# --- Criterio 8: latencia con el perfil ya guardado --------------------------------------------------------


def test_la_oferta_responde_dentro_de_la_meta_de_latencia(entorno):
    """Indicativo (SQLite en memoria, en proceso): la meta de HA02 es p95 ≤ 400 ms y p99 ≤ 800 ms."""
    entorno.enriquecer()
    muestras = []
    for _ in range(60):
        inicio = time.perf_counter()
        assert entorno.oferta().status_code == 200
        muestras.append((time.perf_counter() - inicio) * 1000)

    muestras.sort()
    assert muestras[int(len(muestras) * 0.95) - 1] <= 400
    assert muestras[-1] <= 800, statistics.mean(muestras)
