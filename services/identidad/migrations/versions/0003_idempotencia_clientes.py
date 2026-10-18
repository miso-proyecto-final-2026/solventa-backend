"""Idempotencia del alta de cliente compartida entre réplicas (HU01).

Antes vivía en la memoria de cada proceso: con más de una réplica un reintento podía recibir 409.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "identidad"


def upgrade() -> None:
    op.create_table(
        "idempotencia_clientes",
        sa.Column("clave_hash", sa.String(64), primary_key=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.clientes.id"), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identidad_idempotencia_clientes_expira_en",
        "idempotencia_clientes",
        ["expira_en"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_identidad_idempotencia_clientes_expira_en", table_name="idempotencia_clientes", schema=SCHEMA)
    op.drop_table("idempotencia_clientes", schema=SCHEMA)
