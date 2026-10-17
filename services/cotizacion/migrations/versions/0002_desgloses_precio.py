"""Desglose del precio por cotización, solo inserción (HU14).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "cotizacion"


def upgrade() -> None:
    op.create_table(
        "desgloses_precio",
        sa.Column(
            "cotizacion_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.cotizaciones.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version_esquema", sa.Integer, nullable=False),
        sa.Column("detalle", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    # Inmutabilidad (criterio 5): ni la aplicación ni una consulta suelta pueden
    # modificar o borrar un desglose emitido. Un recálculo es una cotización nueva.
    op.execute(
        f"""
        CREATE FUNCTION "{SCHEMA}".desgloses_precio_inmutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'desgloses_precio es de solo inserción';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_desgloses_precio_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".desgloses_precio
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".desgloses_precio_inmutable()
        """
    )


def downgrade() -> None:
    op.execute(
        f'DROP TRIGGER IF EXISTS trg_desgloses_precio_inmutable ON "{SCHEMA}".desgloses_precio'
    )
    op.execute(f'DROP FUNCTION IF EXISTS "{SCHEMA}".desgloses_precio_inmutable()')
    op.drop_table("desgloses_precio", schema=SCHEMA)
