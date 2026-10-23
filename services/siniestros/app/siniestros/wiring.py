"""Ensamblado de Siniestros desde la configuración del entorno."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.configuracion import permitir_sustituto_local
from app.eventos import (
    ConsumidorEventos,
    PublicadorEventos,
    PublicadorKafka,
    PublicadorMemoria,
    RegistroEventos,
    brokers_desde_entorno,
    parametros_seguridad,
)
from app.siniestros.config import SQLITE_LOCAL, ConfigSiniestros
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


@dataclass
class Ensamblado:
    receptor: Receptor
    engine: Engine
    publicador: PublicadorEventos
    # None solo en desarrollo sin Kafka: el publicador es en memoria y nadie consume.
    consumidor: ConsumidorEventos | None = None
    _tarea: asyncio.Task[None] | None = field(default=None, init=False)

    async def iniciar(self) -> None:
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.iniciar()
        if self.consumidor is not None:
            await self.consumidor.iniciar()
            self._tarea = asyncio.create_task(self.consumidor.ejecutar())

    async def detener(self) -> None:
        if self.consumidor is not None:
            await self.consumidor.detener()
        if self._tarea is not None:
            self._tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarea
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.detener()
        self.engine.dispose()


def construir_ensamblado(config: ConfigSiniestros | None = None) -> Ensamblado:
    config = config or ConfigSiniestros.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local(
            "SINIESTROS_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})"
        )
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    fabrica = sessionmaker(engine, expire_on_commit=False)
    repositorio = RepositorioEventos(fabrica)

    if config.kafka_brokers is None:
        permitir_sustituto_local(
            "KAFKA_BOOTSTRAP_SERVERS", "un publicador en memoria sin consumidor (no hay Kafka)"
        )
        publicador: PublicadorEventos = PublicadorMemoria()
        return Ensamblado(Receptor(repositorio, publicador), engine, publicador)

    brokers = brokers_desde_entorno()
    seguridad = parametros_seguridad()
    publicador = PublicadorKafka(brokers, seguridad, client_id="solventa-siniestros")
    consumidor = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_EVENTOS,
        grupo=GRUPO_RECEPCION,
        manejador=procesar_evento,
        fabrica_sesiones=fabrica,
        registro=RegistroEventos(EVENTOS_PROCESADOS),
        publicador=publicador,
    )
    return Ensamblado(Receptor(repositorio, publicador), engine, publicador, consumidor)
