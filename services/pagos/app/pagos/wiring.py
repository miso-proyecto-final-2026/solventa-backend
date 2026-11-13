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
from app.pagos.conciliacion import ServicioConciliacion
from app.pagos.config import (
    GRUPO_COBROS,
    GRUPO_INDEMNIZACIONES,
    SQLITE_LOCAL,
    TOPIC_COMANDOS,
    TOPIC_COMANDOS_SINIESTROS,
    ConfigPagos,
)
from app.pagos.dominio import PasarelaPago
from app.pagos.gestion import ServicioGestion
from app.pagos.indemnizaciones import ServicioIndemnizaciones
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
    gestion: ServicioGestion
    engine: Engine
    publicador: PublicadorEventos
    relay: RelayEventos
    # None solo en desarrollo sin Kafka: el publicador es en memoria y nadie consume.
    consumidor: ConsumidorEventos | None = None
    indemnizaciones: ServicioIndemnizaciones | None = None
    conciliacion: ServicioConciliacion | None = None
    consumidor_indemnizaciones: ConsumidorEventos | None = None
    detector_intervalo_s: float = 60.0
    _tareas: list[asyncio.Task[None]] = field(default_factory=list, init=False)

    async def iniciar(self) -> None:
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.iniciar()
        if self.consumidor is not None:
            await self.consumidor.iniciar()
            self._tareas.append(asyncio.create_task(self.consumidor.ejecutar()))
        if self.consumidor_indemnizaciones is not None:
            await self.consumidor_indemnizaciones.iniciar()
            self._tareas.append(asyncio.create_task(self.consumidor_indemnizaciones.ejecutar()))
        if self.indemnizaciones is not None:
            self._tareas.append(asyncio.create_task(self.indemnizaciones.ejecutar()))
        self._tareas += [
            asyncio.create_task(self.relay.ejecutar()),
            asyncio.create_task(self.servicio.ejecutar()),
            asyncio.create_task(self.gestion.ejecutar_detector(self.detector_intervalo_s)),
        ]

    async def detener(self) -> None:
        if self.consumidor is not None:
            await self.consumidor.detener()
        if self.consumidor_indemnizaciones is not None:
            await self.consumidor_indemnizaciones.detener()
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
    pasarela = construir_pasarela(config, fabrica)  # una sola pasarela para cobros e indemnizaciones
    servicio = ServicioCobros(
        fabrica=fabrica,
        pasarela=pasarela,
        periodo_gracia_dias=config.periodo_gracia_dias,
        timeout_pasarela_s=config.pasarela_timeout_s,
        reintento_base_s=config.reintento_base_s,
        reintento_maximo_s=config.reintento_maximo_s,
    )
    gestion = ServicioGestion(fabrica=fabrica, cobros=servicio, horas_por_vencer=config.horas_por_vencer)
    indemnizaciones = ServicioIndemnizaciones(
        fabrica=fabrica,
        pasarela=pasarela,
        timeout_pasarela_s=config.pasarela_timeout_s,
        reintento_base_s=config.reintento_base_s,
        reintento_maximo_s=config.reintento_maximo_s,
        max_intentos=config.indemnizacion_max_intentos,
    )
    conciliacion = ServicioConciliacion(fabrica=fabrica, pasarela=pasarela, timeout_pasarela_s=config.pasarela_timeout_s)
    intervalo = config.detector_gracia_intervalo_s

    if config.kafka_brokers is None:
        permitir_sustituto_local("KAFKA_BOOTSTRAP_SERVERS", "un publicador en memoria sin consumidor (no hay Kafka)")
        publicador: PublicadorEventos = PublicadorMemoria()
        return Ensamblado(
            servicio,
            gestion,
            engine,
            publicador,
            RelayEventos(fabrica, publicador),
            indemnizaciones=indemnizaciones,
            conciliacion=conciliacion,
            detector_intervalo_s=intervalo,
        )

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
    consumidor_indemnizaciones = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_COMANDOS_SINIESTROS,
        grupo=GRUPO_INDEMNIZACIONES,
        manejador=indemnizaciones.manejar_evento,
        fabrica_sesiones=fabrica,
        registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS),
        publicador=publicador,
    )
    return Ensamblado(
        servicio,
        gestion,
        engine,
        publicador,
        RelayEventos(fabrica, publicador),
        consumidor,
        indemnizaciones,
        conciliacion,
        consumidor_indemnizaciones,
        detector_intervalo_s=intervalo,
    )
