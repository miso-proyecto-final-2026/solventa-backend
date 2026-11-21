"""Esquema de MS Socios (HU32): clientes_api y contratos_api (ADR-0001). Sin datos de negocio.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "socios"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    op.create_table(
        "clientes_api",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("nombre_socio", sa.String(120), nullable=False),
        sa.Column("client_secret_hash", sa.String(100), nullable=False),
        sa.Column("estado", sa.String(10), nullable=False, server_default="ACTIVO"),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", name="uq_clientes_api_client_id"),
        sa.CheckConstraint("estado IN ('ACTIVO', 'INACTIVO')", name="ck_clientes_api_estado"),
        schema=SCHEMA,
    )
    op.create_table(
        "contratos_api",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "cliente_api_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.clientes_api.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(10), nullable=False),
        sa.Column("endpoints_habilitados", postgresql.JSONB(), nullable=False),
        sa.Column("cuota_rps", sa.Integer(), nullable=False),
        sa.Column("fecha_inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fecha_fin", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("cuota_rps > 0", name="ck_contratos_api_cuota"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_contrato_cliente_version", "contratos_api", ["cliente_api_id", "version", "fecha_inicio"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_contrato_cliente_version", table_name="contratos_api", schema=SCHEMA)
    op.drop_table("contratos_api", schema=SCHEMA)
    op.drop_table("clientes_api", schema=SCHEMA)
