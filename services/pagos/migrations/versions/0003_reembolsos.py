"""Reembolsos de la compensación de la saga (HU21): el libro de la pasarela simulada distingue cobros de reembolsos.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "pagos"


def upgrade() -> None:
    op.add_column(
        "simulador_cargos", sa.Column("tipo", sa.String(10), nullable=False, server_default="COBRO"), schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_column("simulador_cargos", "tipo", schema=SCHEMA)
