"""El contrato publicado (docs/api/openapi.json) coincide con lo que la aplicación expone (HU32: OpenAPI de /partners/v1)."""

import json
from pathlib import Path

from openapi_spec_validator import validate

from app.main import app

RUTA = Path(__file__).parents[1] / "docs" / "api" / "openapi.json"


def test_el_contrato_publicado_es_openapi_valido_y_esta_al_dia():
    publicado = json.loads(RUTA.read_text(encoding="utf-8"))

    validate(publicado)
    assert publicado == app.openapi(), (
        "docs/api/openapi.json está desactualizado: regenérelo con "
        "`python -c \"import json;from app.main import app;"
        "print(json.dumps(app.openapi(),indent=2,ensure_ascii=False))\" > docs/api/openapi.json`"
    )


def test_los_endpoints_de_la_historia_estan_en_el_contrato():
    rutas = app.openapi()["paths"]

    assert "post" in rutas["/oauth/token"]
    assert "post" in rutas["/partners/v1/cotizaciones"]
    assert "get" in rutas["/partners/v1/status"]
    cotizar = rutas["/partners/v1/cotizaciones"]["post"]
    assert {"401", "403", "409", "429"} <= set(cotizar["responses"])
    assert "Retry-After" in cotizar["responses"]["429"]["headers"]
    assert any(p["name"] == "X-Idempotency-Key" for p in cotizar["parameters"])


def _respuestas_de_error():
    for ruta, metodos in app.openapi()["paths"].items():
        if not ruta.startswith("/partners/v1") or ruta.endswith("/status"):
            continue
        for metodo, operacion in metodos.items():
            for codigo, respuesta in operacion["responses"].items():
                if int(codigo) >= 400:
                    yield ruta, metodo, codigo, respuesta


def test_toda_respuesta_de_error_documentada_usa_error_out():
    for ruta, metodo, codigo, respuesta in _respuestas_de_error():
        esquema = respuesta["content"]["application/json"]["schema"]
        assert esquema == {"$ref": "#/components/schemas/ErrorOut"}, (ruta, metodo, codigo)


def test_ninguna_ruta_documenta_el_422_y_las_de_escritura_documentan_el_catalogo():
    for ruta, metodos in app.openapi()["paths"].items():
        if not ruta.startswith("/partners/v1") or ruta.endswith("/status"):
            continue
        for metodo, operacion in metodos.items():
            documentados = set(operacion["responses"])
            assert "422" not in documentados, (ruta, metodo)
            assert {"400", "401", "403", "429", "500", "503"} <= documentados, (ruta, metodo)
            if metodo == "post" and "idempotency" not in ruta and ruta.endswith(("/cotizaciones", "/suscripciones", "/firma")):
                assert "409" in documentados, (ruta, metodo)


def test_code_y_reason_del_esquema_son_exactamente_el_catalogo_y_la_lista_cerrada():
    from app.socios.errores import CATALOGO, MOTIVOS

    cuerpo = app.openapi()["components"]["schemas"]["ErrorCuerpoOut"]["properties"]

    assert set(cuerpo["code"]["enum"]) == set(CATALOGO)
    reason = next(p for p in cuerpo["reason"]["anyOf"] if "enum" in p)
    assert set(reason["enum"]) == set(MOTIVOS)
    assert "reason" not in app.openapi()["components"]["schemas"]["ErrorCuerpoOut"]["required"]


def test_el_retry_after_esta_documentado_en_429_503_y_409():
    cotizar = app.openapi()["paths"]["/partners/v1/cotizaciones"]["post"]["responses"]

    for codigo in ("409", "429", "503"):
        assert "Retry-After" in cotizar[codigo]["headers"]


def test_las_rutas_protegidas_declaran_el_bearer_y_el_estado_es_publico():
    """HA07: el contrato dice cómo autenticarse; antes solo lo decía el README."""
    esquema = app.openapi()
    bearer = esquema["components"]["securitySchemes"]["bearerAuth"]

    assert (bearer["type"], bearer["scheme"], bearer["bearerFormat"]) == ("http", "bearer", "JWT")
    for ruta, metodos in esquema["paths"].items():
        for metodo, operacion in metodos.items():
            if ruta == "/partners/v1/status" or not ruta.startswith("/partners/"):
                assert "security" not in operacion, (ruta, metodo)
            else:
                assert operacion["security"] == [{"bearerAuth": []}], (ruta, metodo)
