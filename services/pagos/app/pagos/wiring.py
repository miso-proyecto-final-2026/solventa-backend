"""Ensamblado de Pagos desde la configuración del entorno.

La pasarela se elige aquí por `PASARELA_PROVEEDOR` (ADR-0004): junto con el adaptador, es lo único que cambia al
sustituir la pasarela. El caso de uso (`servicio.py`) solo conoce el puerto `PasarelaPago`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

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
from app.pagos.config import GRUPO_COBROS, SQLITE_LOCAL, TOPIC_COMANDOS, ConfigPagos
from app.pagos.dominio import PasarelaPago
from app.pagos.modelos_db import TABLA_EVENTOS_PROCESADOS
from app.pagos.relay import RelayEventos
from app.pagos.repositorio import crear_engine, crear_tablas_locales
from app.pagos.servicio import ServicioCobros
from app.pagos.simulador import PasarelaSimulada, modo_desde_texto

PROVEEDORES = ("simulador",)


def construir_pasarela(config: ConfigPagos, fabrica: sessionmaker[Session]) -> PasarelaPago:
    if config.pasarela_proveedor == "simulador":
        return PasarelaSimulada(fabrica, modo=modo_desde_texto(config.simulador_modo), demora_s=config.simulador_demora_s)
    raise ValueError(f"PASARELA_PROVEEDOR={config.pasarela_proveedor!r} no existe; use uno de: {', '.join(PROVEEDORES)}")


@dataclass
class Ensamblado:
    servicio: ServicioCobros
    engine: Engine
    publicador: PublicadorEventos
    relay: RelayEventos
    # None solo en desarrollo sin Kafka: el publicador es en memoria y nadie consume.
    consumidor: ConsumidorEventos | None = None
    _tareas: list[asyncio.Task[None]] = field(default_factory=list, init=False)

    async def iniciar(self) -> None:
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.iniciar()
        if self.consumidor is not None:
            await self.consumidor.iniciar()
            self._tareas.append(asyncio.create_task(self.consumidor.ejecutar()))
        self._tareas += [asyncio.create_task(self.relay.ejecutar()), asyncio.create_task(self.servicio.ejecutar())]

    async def detener(self) -> None:
        if self.consumidor is not None:
            await self.consumidor.detener()
        for tarea in self._tareas:
            tarea.cancel()
        for tarea in self._tareas:
            with contextlib.suppress(asyncio.CancelledError):
                await tarea
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.detener()
        self.engine.dispose()


def construir_ensamblado(config: ConfigPagos | None = None) -> Ensamblado:
    config = config or ConfigPagos.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local("PAGOS_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})")
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    servicio = ServicioCobros(
        fabrica=fabrica,
        pasarela=construir_pasarela(config, fabrica),
        periodo_gracia_dias=config.periodo_gracia_dias,
        timeout_pasarela_s=config.pasarela_timeout_s,
        reintento_base_s=config.reintento_base_s,
        reintento_maximo_s=config.reintento_maximo_s,
    )

    if config.kafka_brokers is None:
        permitir_sustituto_local("KAFKA_BOOTSTRAP_SERVERS", "un publicador en memoria sin consumidor (no hay Kafka)")
        publicador: PublicadorEventos = PublicadorMemoria()
        return Ensamblado(servicio, engine, publicador, RelayEventos(fabrica, publicador))

    brokers = brokers_desde_entorno()
    seguridad = parametros_seguridad()
    publicador = PublicadorKafka(brokers, seguridad, client_id="solventa-pagos")
    consumidor = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_COMANDOS,
        grupo=GRUPO_COBROS,
        manejador=servicio.manejar_evento,
        fabrica_sesiones=fabrica,
        registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS),
        publicador=publicador,
    )
    return Ensamblado(servicio, engine, publicador, RelayEventos(fabrica, publicador), consumidor)
