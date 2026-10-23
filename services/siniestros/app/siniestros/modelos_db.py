"""Tablas de MS Siniestros en su propio esquema `siniestros` (HU27, ADR-0001).

Solo inserción (salvo `eventos_procesados`, de la plantilla de eventos): el evento recibido, su
rechazo y su publicación son evidencia de auditoría; cada hecho nuevo es una fila nueva. Las pólizas se referencian solo por id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.eventos.registro import tabla_eventos_procesados

SCHEMA = "siniestros"


class BaseSiniestros(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class EventoParametricoDB(BaseSiniestros):
    __tablename__ = "eventos_parametricos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evento_id: Mapped[str] = mapped_column(String(64))  # identificador que asigna el proveedor
    fuente: Mapped[str] = mapped_column(String(64))
    tipo: Mapped[str] = mapped_column(String(40))
    datos: Mapped[dict[str, Any]] = mapped_column(JSON)
    ocurrido_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recibido_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estado_validacion: Mapped[str] = mapped_column(String(20))
    poliza_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlacion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Idempotencia (criterio 5): el mismo evento del mismo proveedor no se registra dos veces.
        UniqueConstraint("fuente", "evento_id", name="uq_evento_fuente"),
        Index("ix_eventos_recibido", "recibido_en"),
    )


class EventoRechazadoDB(BaseSiniestros):
    __tablename__ = "eventos_rechazados"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fuente: Mapped[str] = mapped_column(String(64))  # la fuente del llamador autenticado
    evento_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    codigo: Mapped[str] = mapped_column(String(40))
    detalles: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    recibido_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlacion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_rechazados_recibido", "recibido_en"),)


class PublicacionBusDB(BaseSiniestros):
    __tablename__ = "publicaciones_bus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evento_uuid: Mapped[str] = mapped_column(String(36))
    topic: Mapped[str] = mapped_column(String(80))
    publicado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Una sola publicación por evento (criterio 5).
    __table_args__ = (UniqueConstraint("evento_uuid", name="uq_publicacion_evento"),)


# Idempotencia del consumidor (plantilla HS2): la clave (consumidor, event_id) se reserva en la
# misma transacción del manejador. No es de solo inserción: una reentrega incrementa `reentregas`.
EVENTOS_PROCESADOS = tabla_eventos_procesados(BaseSiniestros.metadata, SCHEMA)
