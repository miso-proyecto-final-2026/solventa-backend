"""Ensamblado de pruebas: SQLite en memoria, caché en memoria, proveedores y consentimiento falsos."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.correlacion import MiddlewareCorrelacion
from app.perfilamiento.api import router
from app.perfilamiento.errores import registrar_errores
from app.perfilamiento.repositorio import (
    RepositorioPerfiles,
    crear_engine,
    crear_tablas_locales,
)
from app.perfilamiento.servicio import Enriquecedor
from app.proveedores.cache import CacheMemoria
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import EstadoConsentimiento, Fuente, ProveedorError
from app.proveedores.servicio import ServicioFuentes

CLAVE = "clave-de-prueba"
ORIGEN = "pruebas"
CLIENTE = "cliente-1"

OPEN_FINANCE = {
    "customerId": CLIENTE,
    "monthlyIncomeCop": 4_500_000,
    "debtRatio": 0.32,
    "accountsOpenYears": 6,
    "monthlyObligationsCop": 1_440_000,
    "creditScore": 710,
    "activeProducts": 4,
}
OPEN_DATA = {"documento_ref": CLIENTE, "estrato": 3, "reportes_negativos": 0, "zona_riesgo": 2}


class ProveedorFalso:
    """Proveedor que responde `payload`, o falla con `error`, y cuenta sus llamadas."""

    def __init__(self, payload: dict[str, Any], error: Exception | None = None, espera_s: float = 0):
        self.payload = payload
        self.error = error
        self.espera_s = espera_s
        self.llamadas = 0

    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]:
        self.llamadas += 1
        if self.espera_s:
            await asyncio.sleep(self.espera_s)
        if self.error:
            raise self.error
        return dict(self.payload)

    async def sondear(self) -> None:
        return None


class CacheGrabadora(CacheMemoria):
    """Caché en memoria que además guarda los TTL con que se escribió cada clave."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[Fuente, int] = {}

    async def guardar(self, cliente_id, fuente, payload, ttl_s, ttl_respaldo_s) -> None:
        self.ttls[fuente] = ttl_s
        await super().guardar(cliente_id, fuente, payload, ttl_s, ttl_respaldo_s)


class Entorno:
    """Todo lo que una prueba necesita tocar."""

    def __init__(self) -> None:
        self.open_finance = ProveedorFalso(OPEN_FINANCE)
        self.open_data = ProveedorFalso(OPEN_DATA)
        self.cache = CacheGrabadora()
        self.consentimiento = ConsentimientoStub(EstadoConsentimiento.OTORGADO)
        self.config = ConfigProveedores(timeout_s=0.2, umbral_fallos=3)
        self.engine = crear_engine("sqlite://")
        crear_tablas_locales(self.engine)
        self.repo = RepositorioPerfiles(sessionmaker(self.engine, expire_on_commit=False))
        self.fuentes = ServicioFuentes(
            {Fuente.OPEN_FINANCE: self.open_finance, Fuente.OPEN_DATA: self.open_data},
            self.cache,
            self.consentimiento,
            self.config,
            reloj=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        )
        self.app = FastAPI()
        self.app.include_router(router)
        registrar_errores(self.app)
        self.app.add_middleware(MiddlewareCorrelacion)
        self.app.state.enriquecedor = Enriquecedor(
            self.fuentes, self.repo, reloj=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        )
        self.app.state.api_keys = {CLAVE: ORIGEN}
        self.cliente = TestClient(self.app)
        self.cabeceras = {"X-Service-Key": CLAVE}

    def enriquecer(self, cliente_id: str = CLIENTE, extra: dict[str, str] | None = None):
        return self.cliente.post(
            f"/v1/perfilamiento/{cliente_id}/enriquecer",
            headers={**self.cabeceras, **(extra or {})},
        )

    def perfil(self, cliente_id: str = CLIENTE):
        return self.cliente.get(f"/v1/perfilamiento/{cliente_id}/perfil", headers=self.cabeceras)

    def linaje(self, cliente_id: str = CLIENTE):
        return self.cliente.get(f"/v1/perfilamiento/{cliente_id}/linaje", headers=self.cabeceras)

    def revocar(self, cliente_id: str = CLIENTE) -> None:
        self.consentimiento._estados[cliente_id] = EstadoConsentimiento.REVOCADO


@pytest.fixture
def entorno():
    e = Entorno()
    yield e
    e.engine.dispose()


__all__ = ["CLAVE", "CLIENTE", "ORIGEN", "Entorno", "ProveedorError", "entorno"]
