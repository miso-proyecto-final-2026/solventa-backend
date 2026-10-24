"""Tablas de MS Suscripción en su propio esquema `suscripcion` (ADR-0001: sin claves foráneas hacia otros servicios).

`cotizacion_id` y `cliente_id` son referencias por id. La saga vive en `suscripciones`; lo que se publica a Kafka pasa por
`eventos_salida` (outbox) y lo ya consumido se registra en `eventos_procesados` (idempotencia de consumidores).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.eventos.registro import tabla_eventos_procesados

SCHEMA = "suscripcion"
ESTADOS_QUE_LIBERAN_LA_COTIZACION = ("RECHAZADA", "COMPENSADA")


class BaseSuscripcion(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class SuscripcionDB(BaseSuscripcion):
    __tablename__ = "suscripciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cotizacion_id: Mapped[str] = mapped_column(String(36))
    cliente_id: Mapped[str] = mapped_column(String(64))
    clave_idempotencia: Mapped[str] = mapped_column(String(100))
    estado_saga: Mapped[str] = mapped_column(String(20), default="EN_PROGRESO")
    paso_actual: Mapped[str] = mapped_column(String(20), default="DECISION")
    # Copia de lo que la decisión necesita de la cotización (no se vuelve a consultar al reintentar la decisión).
    producto: Mapped[str] = mapped_column(String(50))
    moneda: Mapped[str] = mapped_column(String(3))
    monto_asegurado: Mapped[int] = mapped_column(BigInteger)
    prima_mensual: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    prima_anual: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    # Token de la pasarela (nunca el PAN). Se borra en cuanto deja de hacer falta (cobro encolado o rechazo).
    metodo_pago_token: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Resultado de la decisión: nulo mientras no se decide.
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    motivos: Mapped[list[str]] = mapped_column(JSON, default=list)
    ajuste: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reglas_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decidido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duracion_decision_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlacion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Criterio 7: la misma clave del mismo cliente devuelve la misma suscripción, también entre réplicas.
        UniqueConstraint("cliente_id", "clave_idempotencia", name="uq_susc_cliente_clave"),
        # Una cotización se suscribe una sola vez; una suscripción rechazada o compensada la libera.
        Index(
            "uq_susc_cotizacion_activa",
            "cotizacion_id",
            unique=True,
            postgresql_where=text("estado_saga NOT IN ('RECHAZADA', 'COMPENSADA')"),
            sqlite_where=text("estado_saga NOT IN ('RECHAZADA', 'COMPENSADA')"),
        ),
        Index("ix_susc_pendientes", "decision", "created_at"),
    )


class RevisionAsistidaDB(BaseSuscripcion):
    """Caso derivado a una persona (HU15 solo lo registra; resolverlo es de otra historia)."""

    __tablename__ = "revisiones_asistidas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suscripcion_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.suscripciones.id"))
    motivo_derivacion: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)  # APROBADA | RECHAZADA | AJUSTADA
    justificacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    ajustes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    asesor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decidido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_revision_suscripcion", "suscripcion_id"),)


class EventoSalidaDB(BaseSuscripcion):
    """Outbox: el evento se guarda en la misma transacción que el cambio de la saga y un proceso aparte lo publica."""

    __tablename__ = "eventos_salida"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    topic: Mapped[str] = mapped_column(String(200))
    clave: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)  # el sobre completo; el token se redacta al publicar
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    publicado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    ultimo_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index(
            "ix_salida_pendientes",
            "id",
            postgresql_where=text("publicado_en IS NULL"),
            sqlite_where=text("publicado_en IS NULL"),
        ),
    )


# Idempotencia de los consumidores (la plantilla de eventos), en el esquema del servicio.
TABLA_EVENTOS_PROCESADOS = tabla_eventos_procesados(BaseSuscripcion.metadata)
