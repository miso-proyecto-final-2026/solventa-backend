"""El contrato publicado (docs/api/openapi.json) coincide con lo que la aplicación expone."""

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


def test_los_dos_endpoints_de_la_historia_estan_en_el_contrato():
    rutas = app.openapi()["paths"]

    assert "post" in rutas["/v1/suscripciones"]
    assert "get" in rutas["/v1/suscripciones/{suscripcion_id}"]
