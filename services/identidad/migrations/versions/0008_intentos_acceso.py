"""Intentos fallidos de acceso para limitar la fuerza bruta (HU05).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13

Una fila por credencial (hash del ámbito y el identificador): cuántos fallos seguidos lleva y hasta cuándo está bloqueada.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = "identidad"


def upgrade() -> None:
    op.create_table(
        "intentos_acceso",
        sa.Column("clave", sa.String(64), primary_key=True),
        sa.Column("fallidos", sa.Integer(), nullable=False),
        sa.Column("bloqueado_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("intentos_acceso", schema=SCHEMA)
