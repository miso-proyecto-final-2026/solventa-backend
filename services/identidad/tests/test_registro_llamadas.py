import logging

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
RUTA = "/v1/clientes/00000000-0000-0000-0000-000000000000/consentimientos/OPEN_FINANCE_OPEN_DATA"


def test_una_llamada_a_un_endpoint_deja_una_linea_con_metodo_ruta_y_estado(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        respuesta = client.get(RUTA)
    lineas = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llamada ")]
    assert len(lineas) == 1
    assert lineas[0].startswith(f"llamada GET {RUTA} -> {respuesta.status_code} (")
    assert lineas[0].endswith(" ms)")


def test_health_no_se_registra(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        client.get("/health")
    assert not [r for r in caplog.records if r.getMessage().startswith("llamada ")]


def test_no_se_registran_cabeceras_ni_cuerpo(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        client.get(RUTA, headers={"Authorization": "Bearer secreto-de-prueba"})
    assert "secreto-de-prueba" not in caplog.text
