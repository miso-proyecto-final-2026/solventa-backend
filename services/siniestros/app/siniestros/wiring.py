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
from app.siniestros.evaluacion import (
    GRUPO_POLIZAS,
    TOPIC_CICLO_VIDA,
    Evaluador,
    RegistroPolizas,
)
from app.siniestros.modelos_db import EVENTOS_PROCESADOS
from app.siniestros.parametrico import cargar_reglas
from app.siniestros.repositorio import (
    RepositorioEventos,
    crear_engine,
    crear_tablas_locales,
)
from app.siniestros.servicio import (
    GRUPO_RECEPCION,
    TOPIC_EVENTOS,
    Receptor,
)


@dataclass
class Ensamblado:
    receptor: Receptor
    engine: Engine
    publicador: PublicadorEventos
    # None solo en desarrollo sin Kafka: el publicador es en memoria y nadie consume.
    consumidor: ConsumidorEventos | None = None
    # Copia local de las pólizas paramétricas (ciclo de vida de la póliza, HU28).
    consumidor_polizas: ConsumidorEventos | None = None
    _tareas: list[asyncio.Task[None]] = field(default_factory=list, init=False)

    @property
    def _consumidores(self) -> list[ConsumidorEventos]:
        return [c for c in (self.consumidor, self.consumidor_polizas) if c is not None]

    async def iniciar(self) -> None:
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.iniciar()
        for consumidor in self._consumidores:
            await consumidor.iniciar()
            self._tareas.append(asyncio.create_task(consumidor.ejecutar()))

    async def detener(self) -> None:
        for consumidor in self._consumidores:
            await consumidor.detener()
        for tarea in self._tareas:
            tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarea
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
    reglas = cargar_reglas()
    registro = RegistroEventos(EVENTOS_PROCESADOS)
    consumidor = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_EVENTOS,
        grupo=GRUPO_RECEPCION,
        manejador=Evaluador(reglas, publicador).procesar,
        fabrica_sesiones=fabrica,
        registro=registro,
        publicador=publicador,
    )
    consumidor_polizas = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_CICLO_VIDA,
        grupo=GRUPO_POLIZAS,
        manejador=RegistroPolizas(reglas).procesar,
        fabrica_sesiones=fabrica,
        registro=registro,
        publicador=publicador,
    )
    return Ensamblado(
        Receptor(repositorio, publicador), engine, publicador, consumidor, consumidor_polizas
    )
