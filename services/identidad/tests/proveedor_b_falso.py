"""Proveedor de KYC «B» falso: implementa el contrato HTTP de B detrás de `httpx.MockTransport`."""

from __future__ import annotations

import asyncio
import json
import uuid
from enum import Enum

import httpx


class ModoB(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"
    HANG = "hang"
    ERROR_503 = "error_503"
    CAIDO = "caido"
    VACIO = "vacio"


class ProveedorBFalso:
    """Abre un caso por `reference` y evalúa el reto como lo haría B (`TURN_LEFT`)."""

    def __init__(self, api_key: str = "clave-b", modo: ModoB = ModoB.PASS) -> None:
        self.api_key = api_key
        self.modo = modo
        self.casos: dict[str, str] = {}
        self.peticiones: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._atender)

    async def _atender(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        if peticion.headers.get("X-Api-Key") != self.api_key:
            return self._error(401, "INVALID_API_KEY")
        if self.modo is ModoB.HANG:
            await asyncio.sleep(3600)
        if self.modo is ModoB.CAIDO:
            raise httpx.ConnectError("conexión rechazada", request=peticion)
        if self.modo is ModoB.ERROR_503:
            return self._error(503, "UNAVAILABLE")
        if self.modo is ModoB.VACIO:
            return httpx.Response(200, json={})
        cuerpo = json.loads(peticion.content)
        caso = self.casos.setdefault(cuerpo["reference"], f"chk_{uuid.uuid4().hex[:12]}")
        if self.modo is ModoB.REVIEW:
            resultado = "REVIEW"
        elif self.modo is ModoB.FAIL or cuerpo["challenge"] != "TURN_LEFT":
            resultado = "FAIL"
        else:
            resultado = "PASS"
        return httpx.Response(200, json={"id": caso, "outcome": resultado, "confidence": 0.97})

    @staticmethod
    def _error(status: int, codigo: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": codigo, "message": "detalle de B"}})
