import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
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


ROL_ASESOR = "ASESOR"
# HU19: gestiona cobros fallidos y recibe el aviso de gracia vencida para evaluar la suspensión.
ROL_OPERADOR = "OPERADOR"
ROLES_INTERNOS = (ROL_ASESOR, ROL_OPERADOR)


class UsuarioInterno(Base):
    """Personal de Solventa: asesores (HU38) y operadores (HU19). Provisional hasta la gestión de usuarios internos (E11)."""

    __tablename__ = "usuarios_internos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    rol: Mapped[str] = mapped_column(String(20))  # ASESOR | OPERADOR (restricción de la migración 0006)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (CheckConstraint("rol IN ('ASESOR', 'OPERADOR')", name="ck_usuarios_internos_rol"),)


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
    """Registro inmutable (solo inserciones) de cada otorgamiento y revocación; la fuente de auditoría."""

    __tablename__ = "consentimiento_registros"
    __table_args__ = (Index("ix_registro_cliente_tipo", "cliente_id", "tipo"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("identidad.clientes.id"))
    tipo: Mapped[str] = mapped_column(String(40))
    evento: Mapped[str] = mapped_column(String(20))  # OTORGADO | REVOCADO
    version_texto: Mapped[str] = mapped_column(String(20))
    canal: Mapped[str] = mapped_column(String(10))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Origen de la solicitud (HU07). Nulos en los registros anteriores a la migración 0005.
    ip_origen: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)


class VerificacionKyc(Base):
    """Un intento de verificación de identidad (HU02). Guarda referencias, nunca las imágenes."""

    __tablename__ = "verificaciones_kyc"
    __table_args__ = (
        UniqueConstraint("cliente_id", "clave_idempotencia", name="uq_verificacion_cliente_clave"),
        Index("ix_verificacion_cliente_creado", "cliente_id", "creado_en"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("identidad.clientes.id"))
    clave_idempotencia: Mapped[str] = mapped_column(String(100))
    estado: Mapped[str] = mapped_column(String(20))  # VERIFICADO | RECHAZADO | EN_REVISION
    motivo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    referencia_proveedor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reto: Mapped[str] = mapped_column(String(30))
    evidencias: Mapped[dict] = mapped_column(JSON)  # parte -> clave en el almacén
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@event.listens_for(ConsentimientoRegistro, "before_update")
@event.listens_for(ConsentimientoRegistro, "before_delete")
def _registro_inmutable(mapper, connection, target):
    raise ValueError("El registro de consentimiento es inmutable: solo admite inserciones")


class IdempotenciaCliente(Base):
    """Resultado de un POST /v1/clientes con `Idempotency-Key`, compartido por todas las réplicas.

    No guarda la clave ni el token de sesión: la clave va con hash y un reintento exitoso recibe una
    sesión nueva sobre el mismo cliente. La fila se escribe en la misma transacción que el cliente.
    """

    __tablename__ = "idempotencia_clientes"

    clave_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    status_code: Mapped[int] = mapped_column(Integer)  # 201 (alta) o 409 (duplicado)
    cliente_id: Mapped[str | None] = mapped_column(ForeignKey("identidad.clientes.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
