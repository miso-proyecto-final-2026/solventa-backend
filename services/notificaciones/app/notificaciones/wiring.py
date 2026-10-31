"""Ensamblado de Notificaciones desde la configuración del entorno.

Los canales se eligen aquí (ADR-0004): el push es siempre el simulado en esta etapa y el correo es el simulado o Amazon SES
según `CORREO_PROVEEDOR`. Los casos de uso (`servicio.py`) solo conocen los puertos `CanalPush` y `CanalCorreo`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

import httpx
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
from app.notificaciones.canales import CorreoSes, CorreoSimulado, PushSimulado
from app.notificaciones.clientes import ClienteIdentidad, ClienteSuscripcion, Identidad
from app.notificaciones.config import (
    CORREO_PROVEEDORES,
    GRUPO_POLIZAS,
    SQLITE_LOCAL,
    TOPIC_CICLO_VIDA,
    ConfigNotificaciones,
)
from app.notificaciones.dominio import CanalCorreo
from app.notificaciones.modelos_db import TABLA_EVENTOS_PROCESADOS
from app.notificaciones.repositorio import crear_engine, crear_tablas_locales
from app.notificaciones.servicio import ServicioNotificaciones


def _http(url: str | None, variable: str, config: ConfigNotificaciones) -> httpx.AsyncClient:
    if not url:
        # Sin sustituto: Notificaciones no tiene sentido sin sus vecinos, ni siquiera en desarrollo.
        raise RuntimeError(f"Falta {variable} (URL del servicio vecino)")
    return httpx.AsyncClient(base_url=url.rstrip("/"), timeout=config.timeout_s)


def construir_correo(config: ConfigNotificaciones) -> CanalCorreo:
    if config.correo_proveedor == "simulado":
        return CorreoSimulado()
    if config.correo_proveedor == "ses":
        return CorreoSes(config.correo_remitente or "", config.correo_region)
    raise ValueError(
        f"CORREO_PROVEEDOR={config.correo_proveedor!r} no existe; use uno de: {', '.join(CORREO_PROVEEDORES)}"
    )


@dataclass
class Ensamblado:
    servicio: ServicioNotificaciones
    identidad: Identidad
    fabrica: sessionmaker[Session]
    engine: Engine
    publicador: PublicadorEventos
    clientes_http: list[httpx.AsyncClient]
    # None solo en desarrollo sin Kafka: nadie entrega eventos.
    consumidor: ConsumidorEventos | None = None
    _tareas: list[asyncio.Task[None]] = field(default_factory=list, init=False)

    async def iniciar(self) -> None:
        if isinstance(self.publicador, PublicadorKafka):
            await self.publicador.iniciar()
        if self.consumidor is not None:
            await self.consumidor.iniciar()
            self._tareas.append(asyncio.create_task(self.consumidor.ejecutar()))
        self._tareas.append(asyncio.create_task(self.servicio.ejecutar()))

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
        for cliente in self.clientes_http:
            await cliente.aclose()
        self.engine.dispose()


def construir_ensamblado(config: ConfigNotificaciones | None = None) -> Ensamblado:
    config = config or ConfigNotificaciones.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local("NOTIFICACIONES_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})")
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)  # en PostgreSQL el esquema lo crea Alembic
    fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    http_identidad = _http(config.identidad_url, "IDENTIDAD_URL", config)
    http_suscripcion = _http(config.suscripcion_url, "SUSCRIPCION_URL", config)
    identidad = ClienteIdentidad(http_identidad, config.identidad_key)
    servicio = ServicioNotificaciones(
        fabrica=fabrica,
        identidad=identidad,
        certificados=ClienteSuscripcion(http_suscripcion, config.suscripcion_key),
        correo=construir_correo(config),
        push=PushSimulado(fabrica),
        url_publica=config.url_publica,
        max_intentos=config.max_intentos,
        espera_base_s=config.reintento_base_s,
        espera_maxima_s=config.reintento_maximo_s,
    )
    clientes = [http_identidad, http_suscripcion]

    if config.kafka_brokers is None:
        permitir_sustituto_local("KAFKA_BOOTSTRAP_SERVERS", "un publicador en memoria sin consumidor (no hay Kafka)")
        return Ensamblado(servicio, identidad, fabrica, engine, PublicadorMemoria(), clientes)

    brokers = brokers_desde_entorno()
    seguridad = parametros_seguridad()
    publicador = PublicadorKafka(brokers, seguridad, client_id="solventa-notificaciones")
    consumidor = ConsumidorEventos(
        brokers=brokers,
        seguridad=seguridad,
        topic=TOPIC_CICLO_VIDA,
        grupo=GRUPO_POLIZAS,
        manejador=servicio.manejar_evento,
        fabrica_sesiones=fabrica,
        registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS),
        publicador=publicador,
    )
    return Ensamblado(servicio, identidad, fabrica, engine, publicador, clientes, consumidor)
