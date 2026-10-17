"""Contrato de cotización: docs/api/cotizacion.openapi.yaml contra la implementación.

El contrato lo consumen móvil, web y API de socios (HU13, HU37, HU38). Estas
pruebas aseguran que el YAML es válido, que sus ejemplos son coherentes y que
las respuestas reales lo cumplen, para que no se desvíen.
"""

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from app.cotizacion.dominio import PerfilUnificado
from app.main import app
from tests.cotizacion.test_api import DISP, VIDA

RUTA = Path(__file__).parents[2] / "docs" / "api" / "cotizacion.openapi.yaml"
SPEC = yaml.safe_load(RUTA.read_text(encoding="utf-8"))
METODOS = {"get", "post", "put"}


def _resolver(nodo):
    """Resuelve un $ref local de primer nivel (#/components/...)."""
    while isinstance(nodo, dict) and "$ref" in nodo:
        destino = SPEC
        for parte in nodo["$ref"].lstrip("#/").split("/"):
            destino = destino[parte]
        nodo = destino
    return nodo


def _validador(esquema) -> Draft202012Validator:
    # Se incrusta `components` para que los $ref internos se resuelvan contra la raíz.
    return Draft202012Validator({**esquema, "components": SPEC["components"]})


def _esquema_respuesta(ruta: str, metodo: str, codigo: str):
    respuesta = _resolver(SPEC["paths"][ruta][metodo]["responses"][codigo])
    return respuesta["content"]["application/json"]["schema"]


def _ejemplos_de_respuesta(ruta: str, metodo: str, codigo: str):
    respuesta = _resolver(SPEC["paths"][ruta][metodo]["responses"][codigo])
    contenido = respuesta["content"]["application/json"]
    if "examples" in contenido:
        return {k: v["value"] for k, v in contenido["examples"].items()}
    return {"example": contenido["example"]} if "example" in contenido else {}


def test_el_contrato_es_openapi_valido():
    validate(SPEC)


def test_las_rutas_y_metodos_del_contrato_son_los_de_la_implementacion():
    documentadas = {
        (m, p) for p, ops in SPEC["paths"].items() for m in ops if m in METODOS
    }
    implementadas = {
        (m, p)
        for p, ops in app.openapi()["paths"].items()
        if p.startswith("/v1/cotizaciones")
        for m in ops
        if m in METODOS
    }

    assert documentadas == implementadas


def test_los_codigos_de_respuesta_del_contrato_son_los_de_la_implementacion():
    generado = app.openapi()["paths"]
    for ruta, ops in SPEC["paths"].items():
        for metodo, op in ops.items():
            if metodo in METODOS:
                assert set(op["responses"]) == set(
                    generado[ruta][metodo]["responses"]
                ), (metodo, ruta)


def test_los_ejemplos_de_cotizacion_cumplen_el_esquema_y_suman_la_prima():
    ejemplos = _ejemplos_de_respuesta("/v1/cotizaciones", "post", "201")
    assert set(ejemplos) == {"normal", "aproximada", "degradada"}
    validador = _validador(_esquema_respuesta("/v1/cotizaciones", "post", "201"))

    for nombre, ejemplo in ejemplos.items():
        validador.validate(ejemplo)
        assert (
            sum(c["monto"] for c in ejemplo["componentes"]) == ejemplo["prima_anual"]
        ), nombre
        assert ejemplo["prima_mensual"] == round(ejemplo["prima_anual"] / 12), nombre
    assert ejemplos["degradada"]["degradado"] is True
    assert (
        ejemplos["aproximada"]["aproximada"] is True and ejemplos["aproximada"]["aviso"]
    )
    assert ejemplos["normal"]["aviso"] is None


@pytest.mark.parametrize("codigo", ["404"])
def test_los_ejemplos_de_error_cumplen_el_esquema(codigo):
    validador = _validador(_esquema_respuesta("/v1/cotizaciones/{id}", "get", codigo))
    for ejemplo in _ejemplos_de_respuesta(
        "/v1/cotizaciones/{id}", "get", codigo
    ).values():
        validador.validate(ejemplo)

    validador_422 = _validador(_esquema_respuesta("/v1/cotizaciones", "post", "422"))
    for ejemplo in _ejemplos_de_respuesta("/v1/cotizaciones", "post", "422").values():
        validador_422.validate(ejemplo)


def test_los_ejemplos_de_solicitud_son_aceptados_y_respuestas_cumplen_el_contrato(
    entorno,
):
    e = entorno()
    cuerpo = SPEC["paths"]["/v1/cotizaciones"]["post"]["requestBody"]["content"][
        "application/json"
    ]
    validador_solicitud = _validador(_resolver(cuerpo["schema"]))
    validador_respuesta = _validador(
        _esquema_respuesta("/v1/cotizaciones", "post", "201")
    )

    for nombre, ejemplo in cuerpo["examples"].items():
        validador_solicitud.validate(ejemplo["value"])
        r = e.client.post("/v1/cotizaciones", json=ejemplo["value"])
        assert r.status_code == 201, nombre
        validador_respuesta.validate(r.json())


@pytest.mark.parametrize(
    "perfil",
    [
        pytest.param(
            PerfilUnificado("x", senales={"ratio_endeudamiento": 0.6}), id="normal"
        ),
        pytest.param(PerfilUnificado("x", con_consentimiento=False), id="aproximada"),
        pytest.param(PerfilUnificado("x", degradado=True), id="degradada"),
    ],
)
@pytest.mark.parametrize("solicitud", [VIDA, DISP], ids=["vida", "dispositivos"])
def test_respuestas_reales_cumplen_el_contrato_en_los_tres_casos(
    entorno, perfil, solicitud
):
    e = entorno(perfil)
    r = e.client.post("/v1/cotizaciones", json=solicitud)
    assert r.status_code == 201
    _validador(_esquema_respuesta("/v1/cotizaciones", "post", "201")).validate(r.json())

    consulta = e.client.get(f"/v1/cotizaciones/{r.json()['id']}")
    _validador(_esquema_respuesta("/v1/cotizaciones/{id}", "get", "200")).validate(
        consulta.json()
    )


def test_errores_reales_cumplen_el_contrato(entorno):
    e = entorno()
    error_validacion = _esquema_respuesta("/v1/cotizaciones", "post", "422")

    producto = e.client.post("/v1/cotizaciones", json={**VIDA, "producto": "x"})
    datos = e.client.post("/v1/cotizaciones", json={**VIDA, "datos": {"edad": 5}})
    forma = e.client.post("/v1/cotizaciones", json={"producto": "x"})
    no_existe = e.client.get("/v1/cotizaciones/no-existe")

    for r in (producto, datos, forma):
        assert r.status_code == 422
        _validador(error_validacion).validate(r.json())
    assert no_existe.status_code == 404
    _validador(_esquema_respuesta("/v1/cotizaciones/{id}", "get", "404")).validate(
        no_existe.json()
    )


def test_los_ejemplos_de_error_del_contrato_coinciden_con_los_mensajes_reales(entorno):
    e = entorno()
    ejemplos = _ejemplos_de_respuesta("/v1/cotizaciones", "post", "422")

    producto = e.client.post("/v1/cotizaciones", json={**VIDA, "producto": "x"}).json()
    edad = e.client.post(
        "/v1/cotizaciones", json={**VIDA, "datos": {**VIDA["datos"], "edad": 66}}
    ).json()

    assert (
        producto["error"]["details"]
        == ejemplos["producto_inexistente"]["error"]["details"]
    )
    assert edad["error"]["details"] == ejemplos["fuera_de_rango"]["error"]["details"]


# --- HU14: desglose de auditoría ------------------------------------------------

RUTA_DESGLOSE = "/v1/cotizaciones/{id}/desglose"


def test_el_ejemplo_del_desglose_cumple_el_esquema_y_suma_la_prima():
    ejemplo = _ejemplos_de_respuesta(RUTA_DESGLOSE, "get", "200")["example"]

    _validador(_esquema_respuesta(RUTA_DESGLOSE, "get", "200")).validate(ejemplo)

    assert sum(c["monto"] for c in ejemplo["componentes"]) == ejemplo["prima_anual"]


def test_el_desglose_declara_la_seguridad_y_la_esquema_de_clave_de_auditor():
    operacion = SPEC["paths"][RUTA_DESGLOSE]["get"]

    assert operacion["security"] == [{"claveAuditor": []}]
    assert SPEC["components"]["securitySchemes"]["claveAuditor"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Service-Key",
        "description": "Clave de servicio con permiso de auditoría (`AUDITOR_API_KEYS`).",
    }


def test_respuestas_reales_del_desglose_cumplen_el_contrato(entorno):
    e = entorno(
        PerfilUnificado(
            "x",
            senales={"ratio_endeudamiento": 0.6},
            fuentes=("open_finance",),
            procedencia={"ratio_endeudamiento": ("open_finance", None)},
        )
    )
    e.client.app.state.auditor_api_keys = ("k",)
    cabecera = {"X-Service-Key": "k"}
    c = e.client.post("/v1/cotizaciones", json=DISP).json()

    ok = e.client.get(f"/v1/cotizaciones/{c['id']}/desglose", headers=cabecera)
    sin_clave = e.client.get(f"/v1/cotizaciones/{c['id']}/desglose")
    sin_permiso = e.client.get(
        f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": "otra"}
    )
    no_existe = e.client.get("/v1/cotizaciones/no-existe/desglose", headers=cabecera)

    assert (ok.status_code, sin_clave.status_code, sin_permiso.status_code) == (
        200,
        401,
        403,
    )
    _validador(_esquema_respuesta(RUTA_DESGLOSE, "get", "200")).validate(ok.json())
    for codigo, respuesta in (
        ("401", sin_clave),
        ("403", sin_permiso),
        ("404", no_existe),
    ):
        _validador(_esquema_respuesta(RUTA_DESGLOSE, "get", codigo)).validate(
            respuesta.json()
        )


def test_los_ejemplos_de_error_del_desglose_coinciden_con_los_mensajes_reales(entorno):
    e = entorno()
    e.client.app.state.auditor_api_keys = ("k",)
    sin_clave = e.client.get("/v1/cotizaciones/x/desglose").json()
    sin_permiso = e.client.get(
        "/v1/cotizaciones/x/desglose", headers={"X-Service-Key": "otra"}
    ).json()

    for codigo, real in (("401", sin_clave), ("403", sin_permiso)):
        ejemplo = _ejemplos_de_respuesta(RUTA_DESGLOSE, "get", codigo)["example"]
        assert (ejemplo["error"]["code"], ejemplo["error"]["message"]) == (
            real["error"]["code"],
            real["error"]["message"],
        )
