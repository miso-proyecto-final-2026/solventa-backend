"""HU23: enriquecimiento, consultas, linaje, resiliencia, caché y revocación."""

from __future__ import annotations

import pytest

from app.proveedores.dominio import EstadoConsentimiento, Fuente, ProveedorError
from tests.perfilamiento.conftest import CLAVE, CLIENTE, ORIGEN

CAMPOS_ESPERADOS = {
    "ingresos_mensuales_cop",
    "ratio_endeudamiento",
    "antiguedad_cuentas_anios",
    "obligaciones_mensuales_cop",
    "score_crediticio",
    "productos_financieros",
    "estrato",
    "reportes_negativos",
    "zona_riesgo",
}


# --- Criterio 1: enriquecer con consentimiento ---------------------------------------------


def test_enriquece_combinando_open_finance_y_open_data(entorno):
    r = entorno.enriquecer()

    assert r.status_code == 201
    cuerpo = r.json()
    assert set(cuerpo["campos"]) == CAMPOS_ESPERADOS
    assert cuerpo["completitud"] == 1.0
    assert cuerpo["degradado"] is False
    assert cuerpo["campos"]["score_crediticio"]["fuente"] == "open_finance"
    assert cuerpo["campos"]["zona_riesgo"]["fuente"] == "open_data"
    assert cuerpo["consentimiento_id"] == f"stub-{CLIENTE}"


def test_el_perfil_no_guarda_identificadores_del_proveedor_ni_el_payload_crudo(entorno):
    cuerpo = entorno.enriquecer().json()

    texto = str(cuerpo)
    assert "customerId" not in texto and "documento_ref" not in texto


# --- Criterio 4: consulta del perfil vigente ------------------------------------------------


def test_consulta_el_perfil_enriquecido_vigente(entorno):
    creado = entorno.enriquecer().json()

    r = entorno.perfil()

    assert r.status_code == 200
    assert r.json()["perfil_id"] == creado["perfil_id"]
    assert r.json()["campos"] == creado["campos"]


def test_un_nuevo_enriquecimiento_crea_otro_perfil_y_el_vigente_es_el_ultimo(entorno):
    primero = entorno.enriquecer().json()["perfil_id"]
    segundo = entorno.enriquecer().json()["perfil_id"]

    assert primero != segundo
    assert entorno.perfil().json()["perfil_id"] == segundo


def test_sin_perfil_previo_la_consulta_responde_404(entorno):
    r = entorno.perfil()

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PERFIL_NO_ENCONTRADO"


# --- Criterio 2: sin consentimiento, sin consulta y con registro ---------------------------


@pytest.mark.parametrize("estado", [EstadoConsentimiento.REVOCADO, EstadoConsentimiento.NO_OTORGADO])
def test_sin_consentimiento_bloquea_no_llama_a_proveedores_y_registra_el_intento(entorno, estado):
    entorno.consentimiento._estados[CLIENTE] = estado

    r = entorno.enriquecer()

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CONSENTIMIENTO_NO_VIGENTE"
    assert entorno.open_finance.llamadas == 0 and entorno.open_data.llamadas == 0
    (intento,) = entorno.repo.intentos_de(CLIENTE)
    assert intento.origen == ORIGEN
    assert intento.operacion == "ENRIQUECER"
    assert intento.estado_consentimiento == estado.value


def test_el_cien_por_ciento_de_los_intentos_bloqueados_queda_registrado(entorno):
    entorno.consentimiento._estados[CLIENTE] = EstadoConsentimiento.REVOCADO

    for _ in range(5):
        assert entorno.enriquecer().status_code == 403
    assert entorno.perfil().status_code == 403

    intentos = entorno.repo.intentos_de(CLIENTE)
    assert len(intentos) == 6
    assert sorted({i.operacion for i in intentos}) == ["CONSULTAR_PERFIL", "ENRIQUECER"]


def test_el_origen_del_intento_es_el_nombre_de_la_clave_que_llamo(entorno):
    entorno.app.state.api_keys = {"otra-clave": "suscripcion", CLAVE: ORIGEN}
    entorno.consentimiento._estados[CLIENTE] = EstadoConsentimiento.NO_OTORGADO

    entorno.cliente.post(
        f"/v1/perfilamiento/{CLIENTE}/enriquecer", headers={"X-Service-Key": "otra-clave"}
    )

    assert entorno.repo.intentos_de(CLIENTE)[0].origen == "suscripcion"


# --- Criterio 9: revocación posterior ------------------------------------------------------


def test_tras_revocar_el_perfil_cacheado_deja_de_entregarse_de_inmediato(entorno):
    entorno.enriquecer()
    assert entorno.perfil().status_code == 200

    entorno.revocar()

    assert entorno.perfil().status_code == 403
    assert entorno.enriquecer().status_code == 403
    assert len(entorno.repo.intentos_de(CLIENTE)) == 2


# --- Criterio 3: linaje --------------------------------------------------------------------


def test_el_linaje_dice_de_que_fuente_salio_cada_dato_cuando_y_con_que_ttl(entorno):
    entorno.enriquecer()

    r = entorno.linaje()

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["consentimiento_id"] == f"stub-{CLIENTE}"
    por_fuente = {f["fuente"]: f for f in cuerpo["fuentes"]}
    assert por_fuente["open_finance"]["ttl_s"] == 3600
    assert por_fuente["open_data"]["ttl_s"] == 86400
    assert "score_crediticio" in por_fuente["open_finance"]["campos"]
    assert "zona_riesgo" in por_fuente["open_data"]["campos"]
    assert por_fuente["open_finance"]["obtenido_en"].startswith("2026-10-21T12:00:00")
    assert not por_fuente["open_finance"]["degradado"]


def test_el_linaje_no_contiene_valores_de_los_datos(entorno):
    entorno.enriquecer()

    texto = str(entorno.linaje().json())

    assert "4500000" not in texto and "710" not in texto


def test_el_linaje_de_un_cliente_sin_perfil_responde_404(entorno):
    assert entorno.linaje().status_code == 404


# --- Criterios 6 y 7: caché y resiliencia --------------------------------------------------


def test_la_segunda_llamada_sale_de_cache_sin_consultar_al_proveedor(entorno):
    entorno.enriquecer()
    r = entorno.enriquecer()

    assert r.json()["hit_cache"] is True
    assert entorno.open_finance.llamadas == 1 and entorno.open_data.llamadas == 1


def test_el_ttl_es_distinto_por_tipo_de_dato_y_lleva_dispersion(entorno):
    entorno.enriquecer()

    of, od = entorno.cache.ttls[Fuente.OPEN_FINANCE], entorno.cache.ttls[Fuente.OPEN_DATA]
    assert 3600 * 0.85 <= of <= 3600 * 1.15 and of != 3600
    assert 86400 * 0.85 <= od <= 86400 * 1.15 and od != 86400


def test_un_proveedor_caido_degrada_sin_fallar_y_no_inventa_datos(entorno):
    entorno.open_finance.error = ProveedorError("503")

    r = entorno.enriquecer()

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["degradado"] is True
    assert "score_crediticio" not in cuerpo["campos"]
    assert cuerpo["ausentes"]["score_crediticio"] == "sin_datos"
    assert "zona_riesgo" in cuerpo["campos"]  # la otra fuente sigue aportando
    of = {f["fuente"]: f for f in entorno.linaje().json()["fuentes"]}["open_finance"]
    assert of["degradado"] is True and of["por_defecto"] is True


def test_un_proveedor_lento_se_corta_por_timeout_y_degrada(entorno):
    entorno.open_data.espera_s = 5

    r = entorno.enriquecer()

    assert r.status_code == 201 and r.json()["degradado"] is True
    assert "estrato" not in r.json()["campos"]


def test_ante_degradacion_se_usa_el_ultimo_valor_cacheado_marcado_como_degradado(entorno):
    entorno.enriquecer()

    async def sin_vigente(*_args):  # la copia vigente expiró; el respaldo de larga duración queda
        return None

    entorno.cache.obtener = sin_vigente
    entorno.open_finance.error = ProveedorError("503")

    r = entorno.enriquecer()

    assert r.status_code == 201 and r.json()["degradado"] is True
    # El respaldo es un dato viejo: se conserva la marca, no el valor.
    assert r.json()["ausentes"]["ingresos_mensuales_cop"] == "degradado"
    of = {f["fuente"]: f for f in entorno.linaje().json()["fuentes"]}["open_finance"]
    assert of["degradado"] is True and of["hit_cache"] is True and of["por_defecto"] is False


def test_el_circuito_se_abre_tras_los_fallos_consecutivos_y_no_sale_a_la_red(entorno):
    entorno.open_finance.error = ProveedorError("503")

    for _ in range(entorno.config.umbral_fallos):
        entorno.enriquecer()
    llamadas = entorno.open_finance.llamadas
    r = entorno.enriquecer()

    assert r.status_code == 201 and r.json()["degradado"] is True
    assert entorno.open_finance.llamadas == llamadas  # circuito abierto: ninguna llamada nueva


# --- Acceso y validación -------------------------------------------------------------------


def test_sin_clave_responde_401_y_con_clave_desconocida_403(entorno):
    url = f"/v1/perfilamiento/{CLIENTE}/perfil"

    assert entorno.cliente.get(url).status_code == 401
    assert entorno.cliente.get(url, headers={"X-Service-Key": "otra"}).status_code == 403


def test_sin_claves_configuradas_nadie_accede(entorno):
    entorno.app.state.api_keys = {}

    assert entorno.perfil().status_code == 403
    assert entorno.enriquecer().status_code == 403


def test_un_cliente_id_invalido_responde_422_con_el_formato_estandar(entorno):
    r = entorno.enriquecer("a b;c")

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["trace_id"]


def test_la_correlacion_viaja_al_perfil_guardado(entorno):
    from sqlalchemy import text

    entorno.enriquecer(extra={"X-Correlation-Id": "corr-123"})

    with entorno.engine.connect() as c:
        valor = c.execute(text("select correlacion_id from perfiles_enriquecidos")).scalar()
    assert valor == "corr-123"
