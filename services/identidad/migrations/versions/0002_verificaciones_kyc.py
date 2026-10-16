"""Verificaciones KYC: un registro por intento de verificación de identidad (HU02).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "identidad"


def upgrade() -> None:
    op.create_table(
        "verificaciones_kyc",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.clientes.id"), nullable=False),
        sa.Column("clave_idempotencia", sa.String(100), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("motivo", sa.String(40), nullable=True),
        sa.Column("referencia_proveedor", sa.String(100), nullable=True),
        sa.Column("reto", sa.String(30), nullable=False),
        sa.Column("evidencias", sa.JSON(), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cliente_id", "clave_idempotencia", name="uq_verificacion_cliente_clave"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_verificacion_cliente_creado",
        "verificaciones_kyc",
        ["cliente_id", "creado_en"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("verificaciones_kyc", schema=SCHEMA)
