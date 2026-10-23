"""Persistencia de Siniestros. Solo inserción y consultas: nada se actualiza ni se borra."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.siniestros.dominio import EventoEntrada
from app.siniestros.modelos_db import (
    SCHEMA,
    BaseSiniestros,
    EventoParametricoDB,
    EventoProcesadoDB,
    EventoRechazadoDB,
    PublicacionBusDB,
    ReentregaEventoDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `siniestros`. En SQLite (dev/pruebas) no hay esquemas."""
    url = normalizar_url(url)
    if url.startswith("sqlite"):
        memoria = ":memory:" in url or url in ("sqlite://", "sqlite:///")
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if memoria else None,
        )
        return engine.execution_options(schema_translate_map={SCHEMA: None})
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


def crear_tablas_locales(engine: Engine) -> None:
    """Solo para SQLite de desarrollo. En PostgreSQL el esquema lo crea Alembic."""
    BaseSiniestros.metadata.create_all(engine)


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


@dataclass(frozen=True)
class EventoGuardado:
    uuid: str
    evento_id: str
    fuente: str
    tipo: str
    datos: dict[str, Any]
    ocurrido_en: datetime
    recibido_en: datetime
    poliza_id: str | None
    correlacion_id: str | None
    publicado: bool

    @property
    def clave_particion(self) -> str:
        """Eventos de la misma póliza van a la misma partición (orden dentro de la póliza)."""
        return self.poliza_id or self.evento_id

    def mensaje(self) -> dict[str, Any]:
        return {
            "evento_uuid": self.uuid,
            "evento_id": self.evento_id,
            "fuente": self.fuente,
            "tipo": self.tipo,
            "datos": self.datos,
            "ocurrido_en": self.ocurrido_en.isoformat(),
            "recibido_en": self.recibido_en.isoformat(),
            "poliza_id": self.poliza_id,
            "correlacion_id": self.correlacion_id,
        }


def _guardado(fila: EventoParametricoDB, publicado: bool) -> EventoGuardado:
    return EventoGuardado(
        uuid=fila.id,
        evento_id=fila.evento_id,
        fuente=fila.fuente,
        tipo=fila.tipo,
        datos=fila.datos,
        ocurrido_en=_utc(fila.ocurrido_en),
        recibido_en=_utc(fila.recibido_en),
        poliza_id=fila.poliza_id,
        correlacion_id=fila.correlacion_id,
        publicado=publicado,
    )


class RepositorioEventos:
    def __init__(self, fabrica: sessionmaker) -> None:
        self._fabrica = fabrica

    def registrar_evento(
        self, evento: EventoEntrada, recibido_en: datetime, correlacion_id: str | None
    ) -> tuple[EventoGuardado, bool]:
        """Devuelve (evento, nuevo). Si ya existía, devuelve el registrado y nuevo=False."""
        with self._fabrica() as sesion:
            fila = EventoParametricoDB(
                id=str(uuid.uuid4()),
                evento_id=evento.evento_id,
                fuente=evento.fuente,
                tipo=evento.tipo,
                datos=evento.datos,
                ocurrido_en=evento.ocurrido_en,
                recibido_en=recibido_en,
                estado_validacion="valido",
                poliza_id=evento.poliza_id,
                correlacion_id=correlacion_id,
            )
            sesion.add(fila)
            try:
                sesion.commit()
            except IntegrityError:
                sesion.rollback()
                existente = sesion.scalars(
                    select(EventoParametricoDB).where(
                        EventoParametricoDB.fuente == evento.fuente,
                        EventoParametricoDB.evento_id == evento.evento_id,
                    )
                ).one()
                publicado = self._publicado(sesion, existente.id)
                return _guardado(existente, publicado), False
            return _guardado(fila, False), True

    @staticmethod
    def _publicado(sesion, evento_uuid: str) -> bool:
        return (
            sesion.scalars(
                select(PublicacionBusDB.id).where(PublicacionBusDB.evento_uuid == evento_uuid)
            ).first()
            is not None
        )

    def esta_publicado(self, evento_uuid: str) -> bool:
        with self._fabrica() as sesion:
            return self._publicado(sesion, evento_uuid)

    def registrar_rechazo(
        self,
        *,
        fuente: str,
        evento_id: str | None,
        codigo: str,
        detalles: list[dict[str, str]],
        recibido_en: datetime,
        correlacion_id: str | None,
    ) -> None:
        with self._fabrica() as sesion:
            sesion.add(
                EventoRechazadoDB(
                    id=str(uuid.uuid4()),
                    fuente=fuente,
                    evento_id=evento_id,
                    codigo=codigo,
                    detalles=detalles,
                    recibido_en=recibido_en,
                    correlacion_id=correlacion_id,
                )
            )
            sesion.commit()

    def registrar_publicacion(
        self, evento_uuid: str, topic: str, particion: int, posicion: int, publicado_en: datetime
    ) -> None:
        with self._fabrica() as sesion:
            sesion.add(
                PublicacionBusDB(
                    evento_uuid=evento_uuid,
                    topic=topic,
                    particion=particion,
                    posicion=posicion,
                    publicado_en=publicado_en,
                )
            )
            try:
                sesion.commit()
            except IntegrityError:  # otra réplica la registró primero: una sola por evento
                sesion.rollback()

    def registrar_procesado(self, evento_uuid: str, consumidor: str, ahora: datetime) -> bool:
        """True si es la primera vez; si es una reentrega la deja registrada y devuelve False."""
        with self._fabrica() as sesion:
            sesion.add(
                EventoProcesadoDB(evento_uuid=evento_uuid, consumidor=consumidor, procesado_en=ahora)
            )
            try:
                sesion.commit()
                return True
            except IntegrityError:
                sesion.rollback()
                sesion.add(
                    ReentregaEventoDB(
                        evento_uuid=evento_uuid, consumidor=consumidor, recibido_en=ahora
                    )
                )
                sesion.commit()
                return False

    def sin_procesar(self, consumidor: str, limite: int = 100_000) -> list[EventoGuardado]:
        """Eventos registrados que `consumidor` aún no procesó, en orden de recepción."""
        with self._fabrica() as sesion:
            procesados = select(EventoProcesadoDB.evento_uuid).where(
                EventoProcesadoDB.consumidor == consumidor
            )
            filas = sesion.scalars(
                select(EventoParametricoDB)
                .where(EventoParametricoDB.id.not_in(procesados))
                .order_by(EventoParametricoDB.recibido_en)
                .limit(limite)
            ).all()
            return [_guardado(f, True) for f in filas]

    def resumen(self, consumidor: str) -> dict[str, int]:
        with self._fabrica() as sesion:

            def contar(modelo, *condiciones) -> int:
                return sesion.scalar(select(func.count()).select_from(modelo).where(*condiciones)) or 0

            return {
                "recibidos": contar(EventoParametricoDB),
                "rechazados": contar(EventoRechazadoDB),
                "publicados": contar(PublicacionBusDB),
                "procesados": contar(EventoProcesadoDB, EventoProcesadoDB.consumidor == consumidor),
                "reentregas": contar(ReentregaEventoDB),
            }
