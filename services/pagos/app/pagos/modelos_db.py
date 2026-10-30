"""Tablas de MS Pagos en su propio esquema `pagos` (ADR-0001: sin claves foráneas hacia otros servicios).

`suscripcion_id` y `cliente_id` son referencias por id. El PAN nunca se guarda: solo el token de la pasarela y los
últimos 4 dígitos (`tokens_tarjeta`, EC-S2.1). Lo que se publica a Kafka pasa por `eventos_salida` (outbox) y lo ya
consumido se registra en `eventos_procesados` (idempotencia de consumidores).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.eventos.registro import tabla_eventos_procesados

SCHEMA = "pagos"


class BasePagos(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class TokenTarjetaDB(BasePagos):
    """Token de la pasarela y lo mínimo para mostrar la tarjeta. Nunca el número completo ni el CVV."""

    __tablename__ = "tokens_tarjeta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(200), unique=True)
    # Nulos hasta que la pasarela describe el token (al tokenizar o antes del primer cobro).
    ultimos4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    marca: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cliente_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TransaccionDB(BasePagos):
    __tablename__ = "transacciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20))
    # Criterio 3: una clave, un cobro. La restricción también vale entre réplicas.
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    suscripcion_id: Mapped[str] = mapped_column(String(36))
    cliente_id: Mapped[str] = mapped_column(String(64))
    monto: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    moneda: Mapped[str] = mapped_column(String(3))
    periodicidad_pago: Mapped[str | None] = mapped_column(String(10), nullable=True)
    estado: Mapped[str] = mapped_column(String(20), default="PENDIENTE")
    token_id: Mapped[int] = mapped_column(Integer, ForeignKey(f"{SCHEMA}.tokens_tarjeta.id"))
    referencia_pasarela: Mapped[str | None] = mapped_column(String(100), nullable=True)
    codigo_rechazo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    motivo_rechazo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Fecha límite del período de gracia tras un rechazo; la gestión posterior es de HU19.
    gracia_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    # Mientras está PENDIENTE: cuándo puede tomarla el procesador. También sirve de «préstamo» mientras una réplica cobra.
    proximo_intento_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origen: Mapped[str] = mapped_column(String(10))  # kafka | rest
    reentregas: Mapped[int] = mapped_column(Integer, default=0)
    correlacion_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    traza_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resuelta_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_tx_pendientes",
            "proximo_intento_en",
            postgresql_where=text("estado = 'PENDIENTE'"),
            sqlite_where=text("estado = 'PENDIENTE'"),
        ),
        Index("ix_tx_suscripcion", "suscripcion_id"),
    )


class IntentoCobroDB(BasePagos):
    """Cada llamada (o consulta) a la pasarela, con su resultado: el rastro de un cobro y de su conciliación."""

    __tablename__ = "intentos_cobro"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaccion_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.transacciones.id"))
    numero: Mapped[int] = mapped_column(Integer)
    resultado: Mapped[str] = mapped_column(String(20))
    codigo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    motivo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    referencia_pasarela: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duracion_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("transaccion_id", "numero", name="uq_intento_numero"),)


class EventoSalidaDB(BasePagos):
    """Outbox: el resultado se guarda en la misma transacción que la transacción de pago y un proceso aparte lo publica."""

    __tablename__ = "eventos_salida"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    topic: Mapped[str] = mapped_column(String(200))
    clave: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)  # el sobre completo; nunca lleva el token
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


class CasoCobroFallidoDB(BasePagos):
    """Un cobro de prima rechazado que gestiona un asesor u operador (HU19).

    El `id` es el de la transacción rechazada que lo abrió. Los reintentos son transacciones nuevas de la misma suscripción
    (`origen = gestion`, clave `cobro_prima:<suscripcion_id>:<n>`) y no extienden el período de gracia.
    """

    __tablename__ = "cobros_fallidos"

    id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.transacciones.id"), primary_key=True)
    suscripcion_id: Mapped[str] = mapped_column(String(36))
    cliente_id: Mapped[str] = mapped_column(String(64))
    monto: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    moneda: Mapped[str] = mapped_column(String(3))
    # El token ya registrado del cliente: el reintento lo reutiliza (no se registra una tarjeta nueva en HU19).
    token_id: Mapped[int] = mapped_column(Integer, ForeignKey(f"{SCHEMA}.tokens_tarjeta.id"))
    estado: Mapped[str] = mapped_column(String(20), default="PENDIENTE")  # PENDIENTE | VENCIDO | RECUPERADO
    gracia_hasta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Del último cobro rechazado (el original o un reintento).
    codigo_rechazo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    motivo_rechazo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ultimo_intento_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reintentos: Mapped[int] = mapped_column(Integer, default=0)
    # El último reintento y la clave de la solicitud que lo pidió: dos clics con la misma clave devuelven el mismo.
    transaccion_vigente_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    clave_solicitud: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operador_notificado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resuelto_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_caso_estado_gracia", "estado", "gracia_hasta"),
        Index("ix_caso_suscripcion", "suscripcion_id"),
    )


class NotaCobroFallidoDB(BasePagos):
    """Nota de seguimiento de un caso, con su autor y fecha (HU19, criterio 5). Nunca lleva datos de tarjeta."""

    __tablename__ = "notas_cobro_fallido"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    caso_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.cobros_fallidos.id"))
    autor_id: Mapped[str] = mapped_column(String(64))
    autor_nombre: Mapped[str] = mapped_column(String(200))
    autor_rol: Mapped[str] = mapped_column(String(20))
    texto: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_nota_caso", "caso_id", "created_at"),)


class CargoSimuladoDB(BasePagos):
    """Libro de la pasarela SIMULADA. Solo lo lee y escribe `simulador.py`; con una pasarela real no existe.

    Vive en la base para que cualquier réplica pueda conciliar un cobro hecho por otra (en memoria no se vería).
    """

    __tablename__ = "simulador_cargos"

    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    referencia: Mapped[str] = mapped_column(String(100))
    monto: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    moneda: Mapped[str] = mapped_column(String(3))
    aprobado: Mapped[bool] = mapped_column(Boolean)
    # COBRO | REEMBOLSO: el libro guarda ambos; «cobros hechos» cuenta solo los primeros.
    tipo: Mapped[str] = mapped_column(String(10), default="COBRO", server_default="COBRO")
    codigo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    motivo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Cuántas órdenes llegaron con esta clave: más de 1 prueba que la idempotencia evitó un cargo doble.
    solicitudes: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Idempotencia de los consumidores (la plantilla de eventos), en el esquema del servicio.
TABLA_EVENTOS_PROCESADOS = tabla_eventos_procesados(BasePagos.metadata)
