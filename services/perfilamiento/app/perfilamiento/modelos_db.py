"""Tablas de MS Perfilamiento en su propio esquema `perfilamiento` (HU23, ADR-0001).

Solo inserción: un nuevo enriquecimiento crea otro perfil, nunca modifica el anterior, y el linaje
y los intentos bloqueados son evidencia de auditoría. El cliente se referencia solo por id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "perfilamiento"


class BasePerfilamiento(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class PerfilEnriquecidoDB(BasePerfilamiento):
    __tablename__ = "perfiles_enriquecidos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cliente_id: Mapped[str] = mapped_column(String(64))  # referencia por id, sin FK
    perfil: Mapped[dict[str, Any]] = mapped_column(JSON)
    completitud: Mapped[float] = mapped_column(Float)
    degradado: Mapped[bool] = mapped_column(Boolean)
    consentimiento_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlacion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_perfiles_cliente_creado", "cliente_id", "creado_en"),)


class LinajeFuenteDB(BasePerfilamiento):
    __tablename__ = "linaje_fuentes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    perfil_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.perfiles_enriquecidos.id", ondelete="CASCADE")
    )
    fuente: Mapped[str] = mapped_column(String(30))
    campos: Mapped[list[str]] = mapped_column(JSON)  # campos canónicos que salieron de esta fuente
    obtenido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ttl_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hit_cache: Mapped[bool] = mapped_column(Boolean)
    degradado: Mapped[bool] = mapped_column(Boolean)
    por_defecto: Mapped[bool] = mapped_column(Boolean)
    consentimiento_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_linaje_perfil_id", "perfil_id"),)


class IntentoBloqueadoDB(BasePerfilamiento):
    __tablename__ = "intentos_bloqueados"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cliente_id: Mapped[str] = mapped_column(String(64))
    operacion: Mapped[str] = mapped_column(String(30))
    origen: Mapped[str] = mapped_column(String(64))  # quién llamó (nombre de la clave de servicio)
    estado_consentimiento: Mapped[str] = mapped_column(String(20))
    ocurrido_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlacion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_intentos_cliente", "cliente_id"),)
