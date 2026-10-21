"""Origen de la cotización: canal, asesor y prospecto sin cuenta (HU38).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "cotizacion"


def upgrade() -> None:
    op.add_column(
        "cotizaciones",
        sa.Column("canal", sa.String(20), nullable=False, server_default="API"),
        schema=SCHEMA,
    )
    op.add_column("cotizaciones", sa.Column("asesor_id", sa.String(36), nullable=True), schema=SCHEMA)
    op.add_column(
        "cotizaciones",
        sa.Column("sin_cuenta", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )
    op.add_column("cotizaciones", sa.Column("documento_tipo", sa.String(20), nullable=True), schema=SCHEMA)
    op.add_column("cotizaciones", sa.Column("documento_numero", sa.String(20), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    for columna in ("documento_numero", "documento_tipo", "sin_cuenta", "asesor_id", "canal"):
        op.drop_column("cotizaciones", columna, schema=SCHEMA)
