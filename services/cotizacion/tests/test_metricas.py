"""Métrica de solicitudes en curso para el HPA (HA03, criterio 4; ADR-0015)."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, Gauge

from app.main import app
from app.metricas import MiddlewareSolicitudesEnCurso


def _app_con_gauge() -> tuple[FastAPI, Gauge, asyncio.Event, asyncio.Event]:
    """App mínima con un endpoint que se queda esperando hasta que la prueba lo libera."""
    gauge = Gauge("en_curso_prueba", "prueba", registry=CollectorRegistry())
    adentro, liberar = asyncio.Event(), asyncio.Event()
    prueba = FastAPI()

    @prueba.get("/lento")
    async def lento() -> dict[str, str]:
        adentro.set()
        await liberar.wait()
        return {"ok": "si"}

    @prueba.get("/falla")
    async def falla() -> None:
        raise RuntimeError("falla de prueba")

    @prueba.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    prueba.add_middleware(MiddlewareSolicitudesEnCurso, gauge=gauge)
    return prueba, gauge, adentro, liberar


def _cliente(prueba: FastAPI) -> httpx.AsyncClient:
    transporte = httpx.ASGITransport(app=prueba, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transporte, base_url="http://prueba")


async def test_cuenta_las_solicitudes_mientras_estan_en_curso():
    prueba, gauge, adentro, liberar = _app_con_gauge()
    async with _cliente(prueba) as cliente:
        tareas = [asyncio.create_task(cliente.get("/lento")) for _ in range(3)]
        await adentro.wait()
        await asyncio.sleep(0.05)  # que las tres entren al endpoint
        assert gauge._value.get() == 3
        liberar.set()
        respuestas = await asyncio.gather(*tareas)
    assert all(r.status_code == 200 for r in respuestas)
    assert gauge._value.get() == 0


async def test_vuelve_a_cero_si_la_solicitud_falla():
    prueba, gauge, _, _ = _app_con_gauge()
    async with _cliente(prueba) as cliente:
        respuesta = await cliente.get("/falla")
    assert respuesta.status_code == 500
    assert gauge._value.get() == 0


async def test_vuelve_a_cero_si_la_solicitud_se_cancela():
    prueba, gauge, adentro, _ = _app_con_gauge()
    async with _cliente(prueba) as cliente:
        tarea = asyncio.create_task(cliente.get("/lento"))
        await adentro.wait()
        assert gauge._value.get() == 1
        tarea.cancel()  # el cliente se desconecta a mitad de la solicitud
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert gauge._value.get() == 0


async def test_no_cuenta_los_sondeos_de_salud():
    prueba, gauge, _, _ = _app_con_gauge()
    gauge.inc = gauge.dec = lambda *a, **k: pytest.fail("/health no debe contar")  # type: ignore[method-assign]
    async with _cliente(prueba) as cliente:
        assert (await cliente.get("/health")).status_code == 200


def test_metrics_expone_el_gauge_en_formato_prometheus():
    respuesta = TestClient(app).get("/metrics")
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/plain")
    assert "# TYPE http_requests_in_flight gauge" in respuesta.text
    # El scrape no se cuenta a sí mismo.
    assert "http_requests_in_flight 0.0" in respuesta.text


def test_metrics_no_aparece_en_el_contrato_openapi():
    assert "/metrics" not in app.openapi()["paths"]
