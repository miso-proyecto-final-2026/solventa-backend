"""Esquema cotizacion: cotizaciones, coberturas y log_latencia (HU12).

Revision ID: 0001
Revises:
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "cotizacion"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    op.create_table(
        "cotizaciones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), nullable=False),
        sa.Column("producto", sa.String(50), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("prima_anual", sa.Numeric(15, 2), nullable=False),
        sa.Column("prima_mensual", sa.Numeric(15, 2), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("version_reglas", sa.String(50), nullable=False),
        sa.Column("degradado", sa.Boolean, nullable=False),
        sa.Column("aproximada", sa.Boolean, nullable=False),
        sa.Column("datos", sa.JSON, nullable=False),
        sa.Column("vigente_hasta", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_cotizaciones_cliente_id", "cotizaciones", ["cliente_id"], schema=SCHEMA
    )

    op.create_table(
        "coberturas",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "cotizacion_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("orden", sa.Integer, nullable=False),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("monto", sa.Numeric(15, 2), nullable=False),
        sa.Column("deducible", sa.Numeric(15, 2), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_coberturas_cotizacion_id", "coberturas", ["cotizacion_id"], schema=SCHEMA
    )

    op.create_table(
        "log_latencia",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "cotizacion_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("latencia_interna_ms", sa.Float, nullable=False),
        sa.Column("latencia_cliente_ms", sa.Float, nullable=True),
        sa.Column("hit_cache", sa.Boolean, nullable=False),
        sa.Column("degradado", sa.Boolean, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_log_latencia_cotizacion_id",
        "log_latencia",
        ["cotizacion_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("log_latencia", schema=SCHEMA)
    op.drop_table("coberturas", schema=SCHEMA)
    op.drop_table("cotizaciones", schema=SCHEMA)
