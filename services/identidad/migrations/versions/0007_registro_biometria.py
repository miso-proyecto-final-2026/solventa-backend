"""Registro del método biométrico activado por el cliente (HU04).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13

Guarda solo qué método se activó, cuándo y si sigue activo; la biometría nunca sale del dispositivo.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SCHEMA = "identidad"


def upgrade() -> None:
    op.create_table(
        "registro_biometria",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey("identidad.clientes.id"), nullable=False, unique=True),
        sa.Column("metodo", sa.String(10), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False),
        sa.Column("activado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("desactivado_en", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("metodo IN ('HUELLA', 'FACIAL')", name="ck_registro_biometria_metodo"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("registro_biometria", schema=SCHEMA)
