"""Tablas de MS Socios en su propio esquema `socios` (ADR-0001: `Cliente_API` y `Contrato_API`, sin datos de negocio).

Socios no guarda cotizaciones ni pólizas (HU32, criterio 9): las pide a Cotización y Suscripción. La cuota y las claves
de idempotencia son estado técnico efímero y viven en Redis, no aquí (ADR-0025).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "socios"
ESTADO_ACTIVO = "ACTIVO"
ESTADO_INACTIVO = "INACTIVO"

_JSON = JSON().with_variant(JSONB(), "postgresql")


class BaseSocios(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class ClienteApiDB(BaseSocios):
    """Un socio de distribución con credenciales OAuth2. Del secreto solo se guarda el hash bcrypt (criterio 11)."""

    __tablename__ = "clientes_api"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    nombre_socio: Mapped[str] = mapped_column(String(120))
    client_secret_hash: Mapped[str] = mapped_column(String(100))
    estado: Mapped[str] = mapped_column(String(10), default=ESTADO_ACTIVO, server_default=ESTADO_ACTIVO)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("client_id", name="uq_clientes_api_client_id"),
        CheckConstraint("estado IN ('ACTIVO', 'INACTIVO')", name="ck_clientes_api_estado"),
    )


class ContratoApiDB(BaseSocios):
    """Qué endpoints de una versión del API tiene habilitados el socio y su cuota de solicitudes por segundo (criterio 4)."""

    __tablename__ = "contratos_api"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cliente_api_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.clientes_api.id", ondelete="CASCADE"))
    version: Mapped[str] = mapped_column(String(10))
    endpoints_habilitados: Mapped[list[Any]] = mapped_column(_JSON)
    cuota_rps: Mapped[int] = mapped_column(Integer)
    fecha_inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fecha_fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_contrato_cliente_version", "cliente_api_id", "version", "fecha_inicio"),
        CheckConstraint("cuota_rps > 0", name="ck_contratos_api_cuota"),
    )
