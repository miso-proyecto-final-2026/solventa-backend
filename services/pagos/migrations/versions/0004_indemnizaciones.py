"""Pago automático de indemnizaciones paramétricas (HU29): pagos por evento–póliza y sus intentos.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "pagos"


def upgrade() -> None:
    op.create_table(
        "indemnizaciones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        sa.Column("evento_id", sa.String(64), nullable=False),
        sa.Column("poliza_id", sa.String(64), nullable=False),
        sa.Column("cliente_id", sa.String(64), nullable=True),
        sa.Column("producto", sa.String(50), nullable=True),
        sa.Column("regla", sa.String(100), nullable=True),
        sa.Column("version_reglas", sa.String(50), nullable=True),
        sa.Column("monto", sa.Numeric(15, 2), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("cuenta_destino", sa.String(200), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("referencia_pasarela", sa.String(100), nullable=True),
        sa.Column("codigo_fallo", sa.String(50), nullable=True),
        sa.Column("motivo_fallo", sa.String(200), nullable=True),
        sa.Column("intentos", sa.Integer, nullable=False, server_default="0"),
        sa.Column("proximo_intento_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reentregas", sa.Integer, nullable=False, server_default="0"),
        sa.Column("correlacion_id", sa.String(100), nullable=True),
        sa.Column("traza_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resuelta_en", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("evento_id", "poliza_id", name="uq_indemnizacion_evento_poliza"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_indemnizaciones_pendientes",
        "indemnizaciones",
        ["proximo_intento_en"],
        schema=SCHEMA,
        postgresql_where=sa.text("estado = 'PENDIENTE'"),
    )
    op.create_table(
        "intentos_pago",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("indemnizacion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.indemnizaciones.id"), nullable=False),
        sa.Column("numero", sa.Integer, nullable=False),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("codigo", sa.String(50), nullable=True),
        sa.Column("motivo", sa.String(200), nullable=True),
        sa.Column("referencia_pasarela", sa.String(100), nullable=True),
        sa.Column("duracion_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("indemnizacion_id", "numero", name="uq_intento_pago_numero"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("intentos_pago", schema=SCHEMA)
    op.drop_index("ix_indemnizaciones_pendientes", table_name="indemnizaciones", schema=SCHEMA)
    op.drop_table("indemnizaciones", schema=SCHEMA)
