import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Cliente(Base):
    """Bounded context Identidad (ADR-0001). Esquema propio, sin FK a otros contextos."""

    __tablename__ = "clientes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    nombre: Mapped[str] = mapped_column(String(200))
    apellido: Mapped[str] = mapped_column(String(200))
    tipo_documento: Mapped[str] = mapped_column(String(20))
    numero_documento: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    celular: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    estado_kyc: Mapped[str] = mapped_column(String(20), default="PENDIENTE")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Sesion(Base):
    """Sesión de un cliente: guarda solo el hash del access_token entregado al registrarse."""

    __tablename__ = "sesiones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("identidad.clientes.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Consentimiento(Base):
    """Estado vigente del consentimiento de un cliente para un tipo (una fila por cliente y tipo)."""

    __tablename__ = "consentimientos"
    __table_args__ = (UniqueConstraint("cliente_id", "tipo", name="uq_consentimiento_cliente_tipo"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("identidad.clientes.id"))
    tipo: Mapped[str] = mapped_column(String(40))
    estado: Mapped[str] = mapped_column(String(20))  # OTORGADO | REVOCADO
    version_texto: Mapped[str] = mapped_column(String(20))
    fecha_otorgamiento: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fecha_revocacion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentimientoRegistro(Base):
    """Registro inmutable (solo inserciones) de cada otorgamiento; la fuente de auditoría."""

    __tablename__ = "consentimiento_registros"
    __table_args__ = (Index("ix_registro_cliente_tipo", "cliente_id", "tipo"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("identidad.clientes.id"))
    tipo: Mapped[str] = mapped_column(String(40))
    evento: Mapped[str] = mapped_column(String(20))  # OTORGADO
    version_texto: Mapped[str] = mapped_column(String(20))
    canal: Mapped[str] = mapped_column(String(10))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@event.listens_for(ConsentimientoRegistro, "before_update")
@event.listens_for(ConsentimientoRegistro, "before_delete")
def _registro_inmutable(mapper, connection, target):
    raise ValueError("El registro de consentimiento es inmutable: solo admite inserciones")
