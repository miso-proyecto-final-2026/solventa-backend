"""Auditoría de las consultas del linaje (HU24): quién consultó el linaje de qué cliente.

Solo inserción, con el mismo disparador `solo_insercion` que crea la revisión 0001.

Revision ID: 0003
Revises: 0002 (ofertas_calculadas, HU25)
Create Date: 2026-11-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "perfilamiento"
TABLA = "consultas_linaje"


def upgrade() -> None:
    op.create_table(
        TABLA,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("perfil_id", sa.String(36), nullable=True),
        sa.Column("origen", sa.String(64), nullable=False),
        sa.Column("consultado_por", sa.String(64), nullable=True),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("consultado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_consultas_linaje_cliente", TABLA, ["cliente_id"], schema=SCHEMA)
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLA}_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".{TABLA}
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".solo_insercion()
        """
    )


def downgrade() -> None:
    op.execute(f'DROP TRIGGER IF EXISTS trg_{TABLA}_inmutable ON "{SCHEMA}".{TABLA}')
    op.drop_table(TABLA, schema=SCHEMA)
