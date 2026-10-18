"""Trazabilidad de consultas a proveedores externos, solo inserción (HU11).

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

SCHEMA = "cotizacion"


def upgrade() -> None:
    op.create_table(
        "trazabilidad_consultas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("proveedor", sa.String(30), nullable=False),
        sa.Column("categorias", sa.JSON, nullable=False),
        sa.Column("consultado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consentimiento_id", sa.String(64), nullable=True),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("latencia_ms", sa.Float, nullable=False),
        sa.Column("hit_cache", sa.Boolean, nullable=False),
        sa.Column("cotizacion_id", sa.String(36), nullable=True),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_trazabilidad_cotizacion_id",
        "trazabilidad_consultas",
        ["cotizacion_id", "consultado_en"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_trazabilidad_cliente_id",
        "trazabilidad_consultas",
        ["cliente_id"],
        schema=SCHEMA,
    )
    # Inmutabilidad (criterio 4): la aplicación solo inserta; la base también lo impone.
    op.execute(
        f"""
        CREATE FUNCTION "{SCHEMA}".trazabilidad_consultas_inmutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'trazabilidad_consultas es de solo inserción';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_trazabilidad_consultas_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".trazabilidad_consultas
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".trazabilidad_consultas_inmutable()
        """
    )


def downgrade() -> None:
    op.execute(
        f'DROP TRIGGER IF EXISTS trg_trazabilidad_consultas_inmutable ON "{SCHEMA}".trazabilidad_consultas'
    )
    op.execute(f'DROP FUNCTION IF EXISTS "{SCHEMA}".trazabilidad_consultas_inmutable()')
    op.drop_table("trazabilidad_consultas", schema=SCHEMA)
