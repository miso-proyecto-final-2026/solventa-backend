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
    event,
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
    # El cliente elige cómo paga (HU18); se cobra el primer período. ANUAL por defecto: la app anterior no lo envía.
    periodicidad_pago: Mapped[str] = mapped_column(String(10), default="ANUAL", server_default="ANUAL")
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
    # Resultado del paso COBRO tal como lo informa MS Pagos (HU18). Nulos mientras no hay respuesta.
    cobro_estado: Mapped[str | None] = mapped_column(String(20), nullable=True)  # APROBADO | RECHAZADO
    cobro_transaccion_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cobro_referencia: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cobro_codigo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cobro_motivo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cobro_gracia_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cobro_resuelto_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Copia de las coberturas [{tipo, monto, deducible}] y de la versión de reglas de la cotización aceptada (HU20):
    # el documento muestra exactamente lo que se ofreció. Nulas en las sagas anteriores a HU20.
    coberturas: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    cotizacion_version_reglas: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Paso FIRMA (HU21): hasta cuándo puede firmar el cliente, cuántos códigos erróneos lleva y, si la firma falla o vence,
    # la compensación del cobro (SOLICITADA mientras MS Pagos reembolsa; la saga queda COMPENSADA al confirmarlo).
    firma_vence_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    firma_intentos_fallidos: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    compensacion_estado: Mapped[str | None] = mapped_column(String(20), nullable=True)
    compensacion_motivo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    compensacion_solicitada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Asesor que hizo la cotización (HU39): decide quién puede gestionar esta saga. Nulo si no es asistida.
    asesor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
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
        Index("ix_susc_asesor", "asesor_id", "created_at"),
    )


class PolizaDB(BaseSuscripcion):
    """Póliza de una suscripción con el cobro confirmado (HU20). Una suscripción tiene a lo sumo una.

    `estado`: PENDIENTE_FIRMA -> VIGENTE (HU21). `documento_estado`: PENDIENTE -> GENERADO | FALLIDO.
    El nombre y el documento del cliente no se guardan aquí: viven solo dentro del PDF.
    """

    __tablename__ = "polizas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    suscripcion_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.suscripciones.id"), unique=True)
    cliente_id: Mapped[str] = mapped_column(String(64))
    numero_poliza: Mapped[str] = mapped_column(String(30), unique=True)
    estado: Mapped[str] = mapped_column(String(20), default="PENDIENTE_FIRMA")
    documento_estado: Mapped[str] = mapped_column(String(20), default="PENDIENTE")
    documento_intentos: Mapped[int] = mapped_column(Integer, default=0)
    documento_proximo_intento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    documento_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    documento_s3_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    documento_generado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SHA-256 del PDF generado: lo que el cliente presenta al firmar (HU21). Nulo en pólizas anteriores a HU21.
    documento_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plantilla_version: Mapped[str] = mapped_column(String(20))
    vigencia_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fecha_vencimiento: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fecha_emision: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_pol_estado", "estado"),
        Index("idx_pol_cliente", "cliente_id"),
        Index(
            "idx_pol_documento_pendiente",
            "documento_proximo_intento",
            postgresql_where=text("documento_estado = 'PENDIENTE'"),
            sqlite_where=text("documento_estado = 'PENDIENTE'"),
        ),
    )


class FirmaDB(BaseSuscripcion):
    """Evidencia de no repudio de la firma de una póliza (HU21). Inmutable: no se actualiza ni se borra.

    Guarda quién firmó (`cliente_id`, no su nombre), qué (hash del documento), cuándo, por qué canal y desde dónde.
    """

    __tablename__ = "firmas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    suscripcion_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.suscripciones.id"), unique=True)
    poliza_id: Mapped[str] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.polizas.id"), unique=True)
    firmante_id: Mapped[str] = mapped_column(String(64))
    documento_sha256: Mapped[str] = mapped_column(String(64))
    firmada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    canal: Mapped[str] = mapped_column(String(10))  # MOVIL | WEB
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    dispositivo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metodo_autenticacion: Mapped[str] = mapped_column(String(20))
    proveedor: Mapped[str] = mapped_column(String(30))
    referencia_proveedor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    copia_firmada_s3_key: Mapped[str] = mapped_column(String(200))
    # Firma asistida (HU39): el firmante sigue siendo el cliente; aquí queda quién lo asistió.
    modalidad: Mapped[str] = mapped_column(String(20), default="CLIENTE", server_default="CLIENTE")
    asesor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@event.listens_for(FirmaDB, "before_update")
@event.listens_for(FirmaDB, "before_delete")
def _firma_inmutable(*_args: object) -> None:
    raise RuntimeError("La evidencia de una firma es inmutable")


class AuditoriaAsesorDB(BaseSuscripcion):
    """Acciones del asesor, también las rechazadas (HU39). Sin código OTP, token de pago ni datos personales. Solo se agrega.

    `suscripcion_id` es nulo si la acción no llegó a tener suscripción (un inicio rechazado): queda `cotizacion_id`.
    """

    __tablename__ = "auditoria_asesor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asesor_id: Mapped[str] = mapped_column(String(36))
    suscripcion_id: Mapped[str | None] = mapped_column(String(36), ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=True)
    cotizacion_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accion: Mapped[str] = mapped_column(String(30))
    resultado: Mapped[str] = mapped_column(String(40))  # EXITO o el código del error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_auditoria_suscripcion", "suscripcion_id"),
        Index("ix_auditoria_asesor", "asesor_id", "created_at"),
    )


@event.listens_for(AuditoriaAsesorDB, "before_update")
@event.listens_for(AuditoriaAsesorDB, "before_delete")
def _auditoria_inmutable(*_args: object) -> None:
    raise RuntimeError("La auditoría del asesor es inmutable")


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
