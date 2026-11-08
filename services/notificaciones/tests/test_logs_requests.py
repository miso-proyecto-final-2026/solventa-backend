"""Cada request entrante deja un log con método, ruta, estado y duración; las sondas y la query no."""

import logging

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.correlacion import MiddlewareCorrelacion


def _cliente():
    async def ok(_):
        return PlainTextResponse("ok")

    async def falla(_):
        return PlainTextResponse("no", status_code=503)

    rutas = [Route("/x", ok), Route("/mal", falla), Route("/health", ok), Route("/metrics", ok)]
    app = Starlette(routes=rutas)
    app.add_middleware(MiddlewareCorrelacion)
    return TestClient(app)


def _requests(caplog):
    return [r.campos for r in caplog.records if getattr(r, "campos", {}).get("evento") == "request_recibido"]


def test_registra_metodo_ruta_estado_y_duracion_sin_la_query(caplog):
    with caplog.at_level(logging.INFO, logger="solventa.http"):
        _cliente().get("/x?token=secreto")
    (campos,) = _requests(caplog)
    assert (campos["metodo"], campos["ruta"], campos["estado"]) == ("GET", "/x", 200)
    assert campos["duracion_ms"] >= 0
    assert "secreto" not in caplog.text


def test_registra_el_estado_de_error(caplog):
    with caplog.at_level(logging.INFO, logger="solventa.http"):
        _cliente().get("/mal")
    assert _requests(caplog)[0]["estado"] == 503


def test_no_registra_health_ni_metrics(caplog):
    with caplog.at_level(logging.INFO, logger="solventa.http"):
        cliente = _cliente()
        cliente.get("/health")
        cliente.get("/metrics")
    assert _requests(caplog) == []
