"""Esquema suscripcion: saga de suscripción, revisiones asistidas, cola de salida de eventos e idempotencia (HU15).

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"
NO_ACTIVAS = "estado_saga NOT IN ('RECHAZADA', 'COMPENSADA')"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    op.create_table(
        "suscripciones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cotizacion_id", sa.String(36), nullable=False),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("clave_idempotencia", sa.String(100), nullable=False),
        sa.Column("estado_saga", sa.String(20), nullable=False),
        sa.Column("paso_actual", sa.String(20), nullable=False),
        sa.Column("producto", sa.String(50), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("monto_asegurado", sa.BigInteger, nullable=False),
        sa.Column("prima_mensual", sa.Numeric(15, 2), nullable=False),
        sa.Column("prima_anual", sa.Numeric(15, 2), nullable=False),
        sa.Column("metodo_pago_token", sa.String(200), nullable=True),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("motivos", sa.JSON, nullable=False),
        sa.Column("ajuste", sa.JSON, nullable=True),
        sa.Column("reglas_version", sa.String(40), nullable=True),
        sa.Column("decidido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duracion_decision_ms", sa.Integer, nullable=True),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cliente_id", "clave_idempotencia", name="uq_susc_cliente_clave"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_susc_cotizacion_activa",
        "suscripciones",
        ["cotizacion_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(NO_ACTIVAS),
    )
    op.create_index("ix_susc_pendientes", "suscripciones", ["decision", "created_at"], schema=SCHEMA)

    op.create_table(
        "revisiones_asistidas",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=False
        ),
        sa.Column("motivo_derivacion", sa.String(200), nullable=False),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("justificacion", sa.Text, nullable=True),
        sa.Column("ajustes", sa.JSON, nullable=True),
        sa.Column("asesor_id", sa.String(64), nullable=True),
        sa.Column("decidido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_revision_suscripcion", "revisiones_asistidas", ["suscripcion_id"], schema=SCHEMA)

    op.create_table(
        "eventos_salida",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("clave", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publicado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intentos", sa.Integer, nullable=False),
        sa.Column("ultimo_error", sa.String(500), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_salida_pendientes",
        "eventos_salida",
        ["id"],
        schema=SCHEMA,
        postgresql_where=sa.text("publicado_en IS NULL"),
    )

    op.create_table(
        "eventos_procesados",
        sa.Column("consumidor", sa.String(100), primary_key=True),
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("procesado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reentregas", sa.Integer, nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("eventos_procesados", schema=SCHEMA)
    op.drop_table("eventos_salida", schema=SCHEMA)
    op.drop_table("revisiones_asistidas", schema=SCHEMA)
    op.drop_table("suscripciones", schema=SCHEMA)
