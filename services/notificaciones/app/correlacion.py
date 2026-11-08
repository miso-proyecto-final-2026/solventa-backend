"""Identificador de correlación y de traza por solicitud (HU11, criterio 5).

Toma `X-Correlation-Id` si viene y es seguro; si no, genera uno. Se devuelve en la
respuesta y queda en `contexto.correlacion_id` para enlazar registros con trazas y logs.
Cada solicitud recibe además un `trace_id` propio (`contexto.traza_id`), el mismo que va en los errores.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.contexto import correlacion_id, traza_id

log = logging.getLogger("solventa.http")
# No se registran las sondas ni las métricas: llenarían Loki sin aportar nada.
_SIN_REGISTRO = frozenset({"/health", "/metrics"})
CABECERA = "X-Correlation-Id"
# Solo caracteres inocuos y longitud acotada: el valor llega de fuera y va a logs y a la base.
_VALIDO = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def normalizar(entrante: str | None) -> str:
    return entrante if entrante and _VALIDO.match(entrante) else str(uuid.uuid4())


class MiddlewareCorrelacion:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        entrante = None
        for nombre, valor in scope["headers"]:
            if nombre == CABECERA.lower().encode():
                entrante = valor.decode("latin-1")
                break
        valor_final = normalizar(entrante)
        token = correlacion_id.set(valor_final)
        token_traza = traza_id.set(str(uuid.uuid4()))

        inicio = time.perf_counter()
        estado = {"codigo": 0}

        async def enviar(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                estado["codigo"] = mensaje["status"]
                mensaje.setdefault("headers", []).append(
                    (CABECERA.lower().encode(), valor_final.encode())
                )
            await send(mensaje)

        try:
            await self.app(scope, receive, enviar)
        finally:
            if scope["path"] not in _SIN_REGISTRO:
                log.info(
                    "request_recibido",
                    extra={"campos": {
                        "evento": "request_recibido",
                        "metodo": scope["method"],
                        "ruta": scope["path"],
                        "estado": estado["codigo"],
                        "duracion_ms": round((time.perf_counter() - inicio) * 1000, 1),
                    }},
                )
            traza_id.reset(token_traza)
            correlacion_id.reset(token)
