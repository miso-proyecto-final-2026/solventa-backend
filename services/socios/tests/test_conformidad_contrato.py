"""Conformidad de MS Socios con su contrato publicado (HA07, criterios 2 y 5).

Schemathesis genera solicitudes a partir de `docs/api/openapi.json` (válidas e inválidas) y valida cada respuesta contra
ese mismo contrato: código documentado, `Content-Type`, encabezados y esquema del cuerpo (ADR-0030). Además, toda
respuesta de error de `/partners/v1` debe traer un `code` del catálogo (EC-I1.2, ADR-0027).

Las solicitudes pasan por el `TestClient` de las demás pruebas, con los dobles de Cotización, Identidad y Suscripción:
no hay red ni contenedores. `SOCIOS_CONTRATO_EJEMPLOS` fija cuántos casos genera cada operación (50 por defecto).
El conteo de fallos provocados queda en `reports/conformidad-errores.json` para el informe de HA07.
"""

import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest
import schemathesis
from app.socios.errores import CATALOGO, MOTIVOS
from hypothesis import HealthCheck, settings
from schemathesis.checks import (
    content_type_conformance,
    missing_required_header,
    negative_data_rejection,
    not_a_server_error,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from tests.conftest import TODOS_LOS_ENDPOINTS
from tests.dobles import CLIENTE_APTO, DOCUMENTO_APTO, _saga

RAIZ = Path(__file__).parents[1]
CONTRATO = json.loads((RAIZ / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
EJEMPLOS = int(os.environ.get("SOCIOS_CONTRATO_EJEMPLOS", "50"))
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

schema = schemathesis.openapi.from_dict(CONTRATO)
FALLOS: Counter[str] = Counter()

# Lo que se exige a cada respuesta. Quedan fuera las verificaciones que piden un 2xx a datos aleatorios
# (`positive_data_acceptance`): un documento o una cotización inventados reciben, con razón, un 400 o un 404.
VERIFICACIONES = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    negative_data_rejection,
    missing_required_header,
]


def _es_error_del_catalogo(ruta: str, status: int) -> bool:
    # `/oauth/token` responde en formato RFC 6749 (ADR-0025) y `/status` tiene su propio esquema: no usan el catálogo.
    return status >= 400 and ruta.startswith("/partners/v1") and not ruta.endswith("/status")


def error_del_catalogo(ctx, response, case):
    """EC-I1.2: todo fallo de `/partners/v1` trae `code` del catálogo, `trace_id` y `X-Correlation-Id`."""
    FALLOS[f"respuestas {case.operation.method.upper()} {case.operation.path} {response.status_code}"] += 1
    if not _es_error_del_catalogo(case.operation.path, response.status_code):
        return
    FALLOS["provocados"] += 1
    error = response.json()["error"]
    assert error["code"] in CATALOGO, f"code fuera del catálogo: {error['code']}"
    # El único código HTTP fuera del catálogo es el 405, que responde `INVALID_REQUEST` con `Allow` (ADR-0027 §11).
    esperado = 405 if response.status_code == 405 and error["code"] == "INVALID_REQUEST" else CATALOGO[error["code"]][0]
    assert esperado == response.status_code, f"{error['code']} no corresponde a {response.status_code}"
    assert error.get("reason") is None or error["reason"] in MOTIVOS, f"reason fuera de la lista: {error['reason']}"
    assert UUID.match(error["trace_id"]), "trace_id no es un UUID"
    assert response.headers.get("x-correlation-id"), "falta X-Correlation-Id"
    FALLOS["con_codigo_del_catalogo"] += 1
    FALLOS[f"{response.status_code} {error['code']}"] += 1


@pytest.fixture(scope="module", autouse=True)
def informe_de_fallos():
    FALLOS.clear()
    yield
    reportes = RAIZ / "reports"
    reportes.mkdir(exist_ok=True)
    resumen = {
        "provocados": FALLOS["provocados"],
        "con_codigo_del_catalogo": FALLOS["con_codigo_del_catalogo"],
        "por_codigo": {k: v for k, v in sorted(FALLOS.items()) if k[0].isdigit()},
        "respuestas_por_operacion": {k.removeprefix("respuestas "): v for k, v in sorted(FALLOS.items()) if k.startswith("respuestas ")},
    }
    (reportes / "conformidad-errores.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")


_TOKEN = {"valor": ""}


@schema.auth(refresh_interval=None)
class BearerDelSocio:
    """Schemathesis pone el `Bearer` (`bearerAuth` del contrato) y lo quita cuando prueba que sin él responde 401."""

    def get(self, case, ctx):
        return _TOKEN["valor"]

    def set(self, case, data, ctx):
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}


@pytest.fixture
def autorizacion(token, identidad):
    """Un socio con todos los endpoints y una cuota que no se agota: lo que se prueba es el contrato, no la cuota."""
    assert DOCUMENTO_APTO in identidad.clientes
    _TOKEN["valor"] = token(endpoints=TODOS_LOS_ENDPOINTS, cuota_rps=10**9)


_AJUSTES = settings(
    max_examples=EJEMPLOS,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.filter_too_much],
    derandomize=True,
)


def _llamar_y_validar(case, cliente):
    kwargs = case.as_transport_kwargs(base_url="http://testserver")
    respuesta = cliente.request(**{k: v for k, v in kwargs.items() if k in _ARGUMENTOS and v})
    case.validate_response(respuesta, checks=VERIFICACIONES, additional_checks=[error_del_catalogo])


_ARGUMENTOS = {"method", "url", "headers", "params", "content", "data", "files", "json", "cookies"}


@schema.include(path_regex="^/partners/v1").parametrize()
@_AJUSTES
def test_las_respuestas_cumplen_el_contrato(case, cliente, autorizacion):
    _llamar_y_validar(case, cliente)


VENTA = "11111111-0000-4000-8000-000000000001"


@pytest.fixture
def venta_del_socio(suscripciones):
    """Una venta del socio de la prueba ya cobrada: así se validan también los esquemas de éxito de cada ruta."""
    suscripciones.sagas[VENTA] = _saga(
        "socio-a", CLIENTE_APTO, 1, "cot-1", estado_saga="COBRO_REALIZADO", paso_actual="FIRMA", _clave="k-0"
    )
    return VENTA


@schema.include(path_regex=r"^/partners/v1/(polizas|suscripciones)/\{suscripcion_id\}").parametrize()
@_AJUSTES
def test_las_respuestas_de_una_venta_existente_cumplen_el_contrato(case, cliente, autorizacion, venta_del_socio):
    if case.path_parameters and "suscripcion_id" in case.path_parameters:
        case.path_parameters["suscripcion_id"] = venta_del_socio
    _llamar_y_validar(case, cliente)
