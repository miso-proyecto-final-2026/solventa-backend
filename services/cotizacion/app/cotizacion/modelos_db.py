"""Tablas de MS Cotización en su propio esquema `cotizacion` (HU12, ADR-0001).

Base y metadatos propios: el esquema evoluciona con Alembic y no comparte
llaves foráneas con otros contextos (el cliente se referencia solo por id).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SCHEMA = "cotizacion"


class BaseCotizacion(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class CotizacionDB(BaseCotizacion):
    __tablename__ = "cotizaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cliente_id: Mapped[str] = mapped_column(String(36))  # referencia por id, sin FK
    producto: Mapped[str] = mapped_column(String(50))
    moneda: Mapped[str] = mapped_column(String(3))
    prima_anual: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    prima_mensual: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    estado: Mapped[str] = mapped_column(String(20))
    version_reglas: Mapped[str] = mapped_column(String(50))
    degradado: Mapped[bool] = mapped_column(Boolean, default=False)
    aproximada: Mapped[bool] = mapped_column(Boolean, default=False)
    datos: Mapped[dict[str, Any]] = mapped_column(JSON)
    vigente_hasta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    coberturas: Mapped[list[CoberturaDB]] = relationship(
        back_populates="cotizacion",
        cascade="all, delete-orphan",
        order_by="CoberturaDB.orden",
    )

    __table_args__ = (Index("ix_cotizaciones_cliente_id", "cliente_id"),)


class CoberturaDB(BaseCotizacion):
    __tablename__ = "coberturas"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cotizacion_id: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE")
    )
    orden: Mapped[int]
    tipo: Mapped[str] = mapped_column(String(50))
    monto: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    deducible: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)

    cotizacion: Mapped[CotizacionDB] = relationship(back_populates="coberturas")

    __table_args__ = (Index("ix_coberturas_cotizacion_id", "cotizacion_id"),)


class DesgloseDB(BaseCotizacion):
    """Desglose del precio de una cotización (HU14). Solo inserción, 1:1 con la cotización.

    `detalle` es JSON versionado (`version_esquema`): ver ADR-0006. En PostgreSQL un
    trigger rechaza UPDATE y DELETE.
    """

    __tablename__ = "desgloses_precio"

    cotizacion_id: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE"), primary_key=True
    )
    version_esquema: Mapped[int]
    detalle: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LogLatenciaDB(BaseCotizacion):
    """Registro técnico por cotización (modelo ajustado tras HA01)."""

    __tablename__ = "log_latencia"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cotizacion_id: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE")
    )
    latencia_interna_ms: Mapped[float] = mapped_column(Float)
    latencia_cliente_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    hit_cache: Mapped[bool] = mapped_column(Boolean)
    degradado: Mapped[bool] = mapped_column(Boolean)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_log_latencia_cotizacion_id", "cotizacion_id"),)
