"""Identificador de correlación por solicitud (HU11, criterio 5).

Toma `X-Correlation-Id` si viene y es seguro; si no, genera uno. Se devuelve en la
respuesta y queda en `contexto.correlacion_id` para enlazar registros con trazas y logs.
"""

from __future__ import annotations

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.contexto import correlacion_id

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

        async def enviar(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                mensaje.setdefault("headers", []).append(
                    (CABECERA.lower().encode(), valor_final.encode())
                )
            await send(mensaje)

        try:
            await self.app(scope, receive, enviar)
        finally:
            correlacion_id.reset(token)
