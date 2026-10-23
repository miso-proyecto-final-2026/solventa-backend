"""Ensamblado de pruebas: SQLite en memoria y bus en memoria con un consumidor manejable."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.correlacion import MiddlewareCorrelacion
from app.siniestros.api import router
from app.siniestros.bus import TOPIC_EVENTOS, BusMemoria, Consumidor
from app.siniestros.errores import registrar_errores
from app.siniestros.repositorio import (
    RepositorioEventos,
    crear_engine,
    crear_tablas_locales,
)
from app.siniestros.servicio import (
    CONSUMIDOR_RECEPCION,
    Receptor,
    RegistradorProcesamiento,
)

CLAVE = "clave-de-prueba"
FUENTE = "proveedor-sismos"
AHORA = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)


def evento(**cambios: Any) -> dict[str, Any]:
    base = {
        "evento_id": "evt-1",
        "tipo": "sismo",
        "fuente": FUENTE,
        "ocurrido_en": "2026-10-23T14:05:00Z",
        "datos": {"magnitud": 6.4, "latitud": 4.71, "longitud": -74.07},
    }
    base.update(cambios)
    return {k: v for k, v in base.items() if v is not None}


async def no_dormir(_segundos: float) -> None:
    return None


class Entorno:
    """Todo lo que una prueba necesita tocar."""

    def __init__(self, capacidad_rezago: int = 1000, url: str = "sqlite://") -> None:
        self.engine = crear_engine(url)
        crear_tablas_locales(self.engine)
        self.repo = RepositorioEventos(sessionmaker(self.engine, expire_on_commit=False))
        self.bus = BusMemoria(particiones=4, capacidad_rezago=capacidad_rezago)
        self.bus.registrar_grupo(TOPIC_EVENTOS, CONSUMIDOR_RECEPCION)
        self.receptor = Receptor(self.repo, self.bus, reloj=lambda: AHORA)
        self.consumidor = Consumidor(
            self.bus,
            TOPIC_EVENTOS,
            CONSUMIDOR_RECEPCION,
            RegistradorProcesamiento(self.repo, reloj=lambda: AHORA),
            espera_base_s=0,
            dormir=no_dormir,
        )
        self.app = FastAPI()
        self.app.include_router(router)
        registrar_errores(self.app)
        self.app.add_middleware(MiddlewareCorrelacion)
        self.app.state.receptor = self.receptor
        self.app.state.api_keys = {CLAVE: FUENTE}
        self.cliente = TestClient(self.app)
        self.cabeceras = {"X-Service-Key": CLAVE}

    def enviar(self, cuerpo: Any = None, cabeceras: dict[str, str] | None = None):
        return self.cliente.post(
            "/v1/siniestros/eventos",
            json=evento() if cuerpo is None else cuerpo,
            headers=self.cabeceras if cabeceras is None else cabeceras,
        )

    def resumen(self) -> dict[str, int]:
        return self.cliente.get("/v1/siniestros/eventos/resumen", headers=self.cabeceras).json()


@pytest.fixture
def entorno():
    e = Entorno()
    yield e
    e.engine.dispose()


@pytest.fixture
def entorno_concurrente(tmp_path):
    """Base SQLite en archivo: la de memoria comparte una sola conexión y no admite hilos en paralelo."""
    e = Entorno(url=f"sqlite:///{tmp_path / 'siniestros.db'}")
    yield e
    e.engine.dispose()
