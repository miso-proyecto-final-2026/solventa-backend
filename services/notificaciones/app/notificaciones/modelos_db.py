"""Tablas de MS Notificaciones en su propio esquema `notificaciones` (ADR-0001: sin claves foráneas hacia otros servicios).

`cliente_id`, `poliza_id` y `suscripcion_id` son referencias por id. El correo del cliente nunca se guarda: se pide a
Identidad en el momento de enviar. Lo ya consumido se registra en `eventos_procesados` (idempotencia de consumidores).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.eventos.registro import tabla_eventos_procesados

SCHEMA = "notificaciones"


class BaseNotificaciones(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class DispositivoDB(BaseNotificaciones):
    """Dispositivo del cliente y su idioma. El identificador lo genera la app (en un push real sería el token de FCM)."""

    __tablename__ = "dispositivos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cliente_id: Mapped[str] = mapped_column(String(64))
    dispositivo_id: Mapped[str] = mapped_column(String(100))
    idioma: Mapped[str] = mapped_column(String(5))
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("cliente_id", "dispositivo_id", name="uq_dispositivo_cliente"),
        Index("ix_dispositivo_cliente", "cliente_id", "actualizado_en"),
    )


class EnvioDB(BaseNotificaciones):
    """Un envío por evento de origen y canal (criterio 5): la restricción única hace que reentregar el evento no envíe dos veces."""

    __tablename__ = "envios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evento_origen_id: Mapped[str] = mapped_column(String(64))
    tipo: Mapped[str] = mapped_column(String(30))
    canal: Mapped[str] = mapped_column(String(10))
    cliente_id: Mapped[str] = mapped_column(String(64))
    idioma: Mapped[str] = mapped_column(String(5))
    # Lo que el mensaje necesita del evento: número, producto, fechas e ids. Sin correo ni datos financieros.
    datos: Mapped[dict[str, Any]] = mapped_column(JSON)
    estado: Mapped[str] = mapped_column(String(10), default="PENDIENTE")
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    proximo_intento_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Solo el nombre de la clase del error: el mensaje puede traer el correo.
    ultimo_error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    referencia_proveedor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlacion_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enviado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("evento_origen_id", "canal", name="uq_envio_evento_canal"),
        Index(
            "ix_envio_pendientes",
            "proximo_intento_en",
            postgresql_where=text("estado = 'PENDIENTE'"),
            sqlite_where=text("estado = 'PENDIENTE'"),
        ),
        Index("ix_envio_fallidos", "estado", "creado_en"),
    )


class BandejaPushDB(BaseNotificaciones):
    """La bandeja del push simulado: lo que el adaptador «entrega». La app la consulta y lanza la notificación del sistema."""

    __tablename__ = "bandeja_push"

    envio_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.envios.id"), primary_key=True)
    cliente_id: Mapped[str] = mapped_column(String(64))
    tipo: Mapped[str] = mapped_column(String(30))
    suscripcion_id: Mapped[str] = mapped_column(String(36))
    titulo: Mapped[str] = mapped_column(String(120))
    texto: Mapped[str] = mapped_column(String(300))
    titulo_publico: Mapped[str] = mapped_column(String(120))
    texto_publico: Mapped[str] = mapped_column(String(300))
    prioridad: Mapped[str] = mapped_column(String(10))
    idioma: Mapped[str] = mapped_column(String(5))
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recibida_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_bandeja_cliente", "cliente_id", "creado_en"),)


# Idempotencia de los consumidores (la plantilla de eventos), en el esquema del servicio.
TABLA_EVENTOS_PROCESADOS = tabla_eventos_procesados(BaseNotificaciones.metadata)
