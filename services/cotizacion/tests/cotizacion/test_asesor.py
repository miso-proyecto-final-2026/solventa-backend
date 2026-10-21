"""Cotización asistida por un asesor (HU38): acceso, perfil, catálogo y consistencia de precio."""

import time

import jwt
import pytest

from app.acceso import VerificadorJwtHs256
from app.cotizacion.api_asesor import router as router_asesor
from app.cotizacion.api_catalogo import construir_catalogo
from app.cotizacion.api_catalogo import router as router_catalogo
from app.cotizacion.dominio import PerfilUnificado
from app.cotizacion.niveles import ClasificadorSenales

from .test_api import AHORA, VIDA

SECRETO = "secreto-de-prueba-con-32-bytes-o-mas!"
EMISOR = "solventa-identidad"
ASESOR_ID = "asesor-1"

DATOS_VIDA = {"edad": 35, "suma_asegurada": 50_000_000}
ASISTIDA_CLIENTE = {"cliente_id": "cli-1", "producto": "microseguro_vida", "datos": DATOS_VIDA}
ASISTIDA_SIN_CUENTA = {
    "documento": {"tipo": "CC", "numero": "55667788"},
    "producto": "microseguro_vida",
    "datos": DATOS_VIDA,
}


def _token(rol="ASESOR", vence_en=600, secreto=SECRETO):
    ahora = int(time.time())
    return jwt.encode(
        {"sub": ASESOR_ID, "rol": rol, "nombre": "Asesora", "iss": EMISOR, "iat": ahora, "exp": ahora + vence_en},
        secreto,
        algorithm="HS256",
    )


def _auth(**kw):
    return {"Authorization": f"Bearer {_token(**kw)}"}


@pytest.fixture
def asesor_env(entorno, reglas):
    """Entorno de cotización con los routers del asesor y el catálogo montados."""

    def _crear(perfil=None, **kw):
        e = entorno(perfil, **kw) if perfil is not None else entorno(**kw)
        app = e.client.app
        app.include_router(router_asesor)
        app.include_router(router_catalogo)
        app.state.verificador_asesor = VerificadorJwtHs256(SECRETO, EMISOR)
        app.state.perfiles = e.perfiles
        app.state.clasificador_senales = ClasificadorSenales.desde_archivo()
        app.state.catalogo = construir_catalogo(reglas)
        return e

    return _crear


# --- acceso -----------------------------------------------------------------


def test_sin_token_se_deniega_con_401(asesor_env):
    r = asesor_env().client.post("/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NO_AUTENTICADO"


def test_token_vencido_o_de_otro_emisor_se_deniega_con_401(asesor_env):
    e = asesor_env()
    for token in (_token(vence_en=-10), _token(secreto="otro-secreto-con-32-bytes-o-mas-xx")):
        r = e.client.post(
            "/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401


def test_un_rol_distinto_de_asesor_se_deniega_con_403(asesor_env):
    r = asesor_env().client.post(
        "/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE, headers=_auth(rol="AUDITOR")
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "SIN_PERMISO"


def test_el_perfil_tambien_exige_el_rol_de_asesor(asesor_env):
    e = asesor_env()
    assert e.client.get("/v1/asesor/clientes/cli-1/perfil").status_code == 401
    assert e.client.get("/v1/asesor/clientes/cli-1/perfil", headers=_auth(rol="CLIENTE")).status_code == 403


# --- cotización asistida ----------------------------------------------------


def test_el_precio_asistido_coincide_con_el_de_la_api_para_la_misma_entrada(asesor_env):
    e = asesor_env(reloj=lambda: AHORA)

    api = e.client.post("/v1/cotizaciones", json=VIDA).json()
    asistida = e.client.post("/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE, headers=_auth()).json()

    assert asistida["prima_anual"] == api["prima_anual"]
    assert asistida["prima_mensual"] == api["prima_mensual"]
    assert asistida["componentes"] == api["componentes"]
    assert asistida["coberturas"] == api["coberturas"]


def test_cotizar_a_un_cliente_registra_canal_y_asesor(asesor_env):
    e = asesor_env()

    c = e.client.post("/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE, headers=_auth()).json()

    assert (c["canal"], c["sin_cuenta"], c["perfil_fuente"]) == ("WEB_ASESOR", False, "REAL")
    fila = e.filas("cotizaciones")[0]
    assert (fila["canal"], fila["asesor_id"], fila["cliente_id"]) == ("WEB_ASESOR", ASESOR_ID, "cli-1")
    assert e.perfiles.llamadas == 1


def test_sin_cuenta_se_cotiza_con_perfil_por_defecto_y_sin_consultar_el_perfil(asesor_env):
    e = asesor_env()

    r = e.client.post("/v1/asesor/cotizaciones", json=ASISTIDA_SIN_CUENTA, headers=_auth())

    assert r.status_code == 201
    c = r.json()
    assert (c["aproximada"], c["perfil_fuente"], c["sin_cuenta"]) == (True, "POR_DEFECTO", True)
    assert c["aviso"]
    assert e.perfiles.llamadas == 0
    fila = e.filas("cotizaciones")[0]
    assert (fila["sin_cuenta"], fila["documento_tipo"], fila["documento_numero"]) == (1, "CC", "55667788")
    # La cotización de un prospecto se puede consultar por el contrato de siempre.
    assert e.client.get(f"/v1/cotizaciones/{c['id']}").status_code == 200


def test_perfil_degradado_se_informa_como_degradado(asesor_env):
    e = asesor_env(PerfilUnificado("x", degradado=True))

    c = e.client.post("/v1/asesor/cotizaciones", json=ASISTIDA_CLIENTE, headers=_auth()).json()

    assert c["perfil_fuente"] == "DEGRADADO"


@pytest.mark.parametrize(
    "extra",
    [
        {"cliente_id": "cli-1", "documento": {"tipo": "CC", "numero": "1234567"}},
        {},
    ],
    ids=["ambos", "ninguno"],
)
def test_debe_venir_exactamente_uno_de_cliente_id_o_documento(asesor_env, extra):
    cuerpo = {"producto": "microseguro_vida", "datos": DATOS_VIDA, **extra}

    r = asesor_env().client.post("/v1/asesor/cotizaciones", json=cuerpo, headers=_auth())

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_datos_fuera_de_rango_devuelven_el_detalle_por_campo(asesor_env):
    cuerpo = {**ASISTIDA_CLIENTE, "datos": {"edad": 5, "suma_asegurada": 50_000_000}}

    r = asesor_env().client.post("/v1/asesor/cotizaciones", json=cuerpo, headers=_auth())

    assert r.status_code == 422
    assert any(d["campo"].endswith("edad") for d in r.json()["error"]["details"])


# --- resumen de perfil ------------------------------------------------------


def test_el_perfil_muestra_niveles_y_no_cifras(asesor_env):
    e = asesor_env()

    r = e.client.get("/v1/asesor/clientes/cli-1/perfil", headers=_auth())

    assert r.status_code == 200
    p = r.json()
    assert (p["con_consentimiento"], p["perfil_fuente"]) == (True, "REAL")
    assert {s["nombre"]: s["nivel"] for s in p["senales"]} == {
        "ratio_endeudamiento": "BAJO",
        "reportes_negativos": "BAJO",
        "ingresos_mensuales_cop": "MEDIO",
    }
    assert "4500000" not in r.text and "0.32" not in r.text


def test_sin_consentimiento_el_perfil_es_por_defecto_y_sin_senales(asesor_env):
    e = asesor_env(PerfilUnificado("x", con_consentimiento=False))

    p = e.client.get("/v1/asesor/clientes/cli-1/perfil", headers=_auth()).json()

    assert (p["con_consentimiento"], p["perfil_fuente"], p["senales"]) == (False, "POR_DEFECTO", [])


# --- clasificador -----------------------------------------------------------


@pytest.mark.parametrize(
    "senal,valor,esperado",
    [
        ("ratio_endeudamiento", 0.34, "BAJO"),
        ("ratio_endeudamiento", 0.35, "MEDIO"),
        ("ratio_endeudamiento", 0.50, "ALTO"),
        ("reportes_negativos", 0, "BAJO"),
        ("reportes_negativos", 1, "MEDIO"),
        ("reportes_negativos", 3, "ALTO"),
        ("ingresos_mensuales_cop", 6_000_000, "BAJO"),
        ("ingresos_mensuales_cop", 5_999_999, "MEDIO"),
        ("ingresos_mensuales_cop", 1_999_999, "ALTO"),
    ],
)
def test_el_clasificador_respeta_los_cortes_en_sus_bordes(senal, valor, esperado):
    assert ClasificadorSenales.desde_archivo().clasificar(senal, valor) == esperado


def test_una_senal_sin_cortes_no_se_clasifica():
    c = ClasificadorSenales.desde_archivo()
    assert c.clasificar("desconocida", 1) is None
    assert c.clasificar_todas({"desconocida": 1}) == []


# --- catálogo ---------------------------------------------------------------


def test_el_catalogo_lista_los_productos_con_sus_campos(asesor_env):
    r = asesor_env().client.get("/v1/productos")

    assert r.status_code == 200
    cat = r.json()
    vida = next(p for p in cat["productos"] if p["codigo"] == "microseguro_vida")
    edad = next(c for c in vida["campos"] if c["nombre"] == "edad")
    assert (edad["tipo"], edad["min"], edad["max"]) == ("entero", 18, 65)
    disp = next(p for p in cat["productos"] if p["codigo"] == "proteccion_dispositivos")
    tipo = next(c for c in disp["campos"] if c["nombre"] == "tipo_dispositivo")
    assert tipo["valores"] == ["celular", "portatil", "tableta"]
    assert r.headers["etag"] == f'"{cat["version_reglas"]}"'
    assert "max-age" in r.headers["cache-control"]


def test_el_catalogo_responde_304_si_no_cambio(asesor_env):
    e = asesor_env()
    etag = e.client.get("/v1/productos").headers["etag"]

    r = e.client.get("/v1/productos", headers={"If-None-Match": etag})

    assert r.status_code == 304
    assert r.content == b""


# --- contrato ---------------------------------------------------------------


def test_las_respuestas_reales_del_asesor_cumplen_el_contrato(asesor_env):
    from .test_contrato import _esquema_respuesta, _validador

    e = asesor_env()
    cotizacion = _validador(_esquema_respuesta("/v1/asesor/cotizaciones", "post", "201"))
    perfil = _validador(_esquema_respuesta("/v1/asesor/clientes/{cliente_id}/perfil", "get", "200"))
    catalogo = _validador(_esquema_respuesta("/v1/productos", "get", "200"))

    for cuerpo in (ASISTIDA_CLIENTE, ASISTIDA_SIN_CUENTA):
        r = e.client.post("/v1/asesor/cotizaciones", json=cuerpo, headers=_auth())
        cotizacion.validate(r.json())
    perfil.validate(e.client.get("/v1/asesor/clientes/cli-1/perfil", headers=_auth()).json())
    catalogo.validate(e.client.get("/v1/productos").json())
