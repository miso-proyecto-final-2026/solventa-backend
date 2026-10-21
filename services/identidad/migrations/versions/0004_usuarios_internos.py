"""Usuarios internos (asesores) para la cotización asistida (HU38).

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

SCHEMA = "identidad"


def upgrade() -> None:
    op.create_table(
        "usuarios_internos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("rol", sa.String(20), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identidad_usuarios_internos_email",
        "usuarios_internos",
        ["email"],
        unique=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_identidad_usuarios_internos_email", table_name="usuarios_internos", schema=SCHEMA)
    op.drop_table("usuarios_internos", schema=SCHEMA)
