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
