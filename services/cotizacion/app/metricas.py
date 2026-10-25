"""Solicitudes en curso para el autoescalado (HA03, criterio 4; ADR-0014).

El HPA de Cotización escala por solicitudes concurrentes, no por CPU: el caché desacopla la CPU
del volumen (hallazgo de HA02). Este módulo cuenta las solicitudes HTTP en curso en el proceso y
las expone en `/metrics` (formato de Prometheus); Prometheus las recoge y el adaptador se las
entrega al HPA como métrica por pod.

Un solo proceso por pod (uvicorn sin workers), así que un `Gauge` en memoria basta. `/metrics` y
`/health` no cuentan: los sondeos y el scrape no son carga de usuario.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from starlette.types import ASGIApp, Receive, Scope, Send

RUTA_METRICAS = "/metrics"
EXCLUIDAS = frozenset({RUTA_METRICAS, "/health"})

# Registro propio: solo expone lo que el autoescalado necesita.
REGISTRO = CollectorRegistry()
SOLICITUDES_EN_CURSO = Gauge(
    "http_requests_in_flight",
    "Solicitudes HTTP que el proceso está atendiendo en este momento.",
    registry=REGISTRO,
)


class MiddlewareSolicitudesEnCurso:
    def __init__(self, app: ASGIApp, gauge: Gauge = SOLICITUDES_EN_CURSO) -> None:
        self.app = app
        self.gauge = gauge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXCLUIDAS:
            await self.app(scope, receive, send)
            return
        self.gauge.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            # También si la solicitud falla o el cliente se desconecta: si no, el HPA vería carga fantasma.
            self.gauge.dec()


router = APIRouter()


@router.get(RUTA_METRICAS, include_in_schema=False)
def metricas() -> Response:
    return Response(generate_latest(REGISTRO), media_type=CONTENT_TYPE_LATEST)
