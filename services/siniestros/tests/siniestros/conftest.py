"""Ensamblado de pruebas: SQLite en memoria y publicador en memoria (el cliente de eventos de la plantilla)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiokafka.errors import KafkaConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.correlacion import MiddlewareCorrelacion
from app.eventos import (
    ConsumidorEventos,
    Evento,
    PoliticaReintentos,
    PublicadorMemoria,
    RegistroEventos,
    parametros_seguridad,
)
from app.siniestros.api import router
from app.siniestros.errores import registrar_errores
from app.siniestros.modelos_db import EVENTOS_PROCESADOS
from app.siniestros.repositorio import (
    RepositorioEventos,
    crear_engine,
    crear_tablas_locales,
)
from app.siniestros.servicio import (
    GRUPO_RECEPCION,
    TOPIC_EVENTOS,
    Receptor,
    procesar_evento,
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


class PublicadorQueFalla(PublicadorMemoria):
    """Como el de memoria, pero Kafka puede estar caído (`caido = True`)."""

    def __init__(self) -> None:
        super().__init__()
        self.caido = False

    async def publicar(self, topic: str, evento: Evento, clave: str) -> None:
        if self.caido:
            raise KafkaConnectionError("broker caído")
        await super().publicar(topic, evento, clave)


def mensaje_de(topic: str, clave: str, evento_publicado: Evento, offset: int = 0) -> SimpleNamespace:
    """Lo que `ConsumidorEventos.procesar` lee de un registro de Kafka."""
    return SimpleNamespace(
        topic=topic,
        key=clave.encode(),
        value=evento_publicado.a_bytes(),
        partition=0,
        offset=offset,
    )


class Entorno:
    """Todo lo que una prueba necesita tocar."""

    def __init__(self, url: str = "sqlite://") -> None:
        self.engine = crear_engine(url)
        crear_tablas_locales(self.engine)
        self.fabrica = sessionmaker(self.engine, expire_on_commit=False)
        self.repo = RepositorioEventos(self.fabrica)
        self.publicador = PublicadorQueFalla()
        self.receptor = Receptor(self.repo, self.publicador, reloj=lambda: AHORA)
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

    def consumidor(self, manejador=procesar_evento, **kw) -> ConsumidorEventos:
        """El consumidor real de la plantilla, sin conectarse: las pruebas llaman a `procesar`."""
        return ConsumidorEventos(
            brokers=["sin-kafka:9092"],
            seguridad=parametros_seguridad("PLAINTEXT"),
            topic=TOPIC_EVENTOS,
            grupo=GRUPO_RECEPCION,
            manejador=manejador,
            fabrica_sesiones=self.fabrica,
            registro=RegistroEventos(EVENTOS_PROCESADOS),
            publicador=self.publicador,
            politica=kw.pop("politica", PoliticaReintentos(intentos=3, base_s=0.01)),
            dormir=no_dormir,
        )

    async def entregar_todo(self, consumidor: ConsumidorEventos) -> None:
        """Entrega al consumidor lo publicado, como lo haría Kafka."""
        for i, (topic, clave, publicado) in enumerate(list(self.publicador.publicados)):
            await consumidor.procesar(mensaje_de(topic, clave, publicado, offset=i))


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
