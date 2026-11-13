"""Revisión asistida (HU16): decisión del asesor, vigencia de la cotización en la saga e inmutabilidad de lo decidido.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def upgrade() -> None:
    op.add_column("revisiones_asistidas", sa.Column("asesor_nombre", sa.String(120), nullable=True), schema=SCHEMA)
    op.add_column(
        "suscripciones", sa.Column("cotizacion_vigente_hasta", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA
    )
    # Un solo caso abierto por saga, aunque dos réplicas intenten crearlo.
    op.create_index(
        "uq_revision_abierta",
        "revisiones_asistidas",
        ["suscripcion_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("decision IS NULL"),
    )
    op.create_index("ix_revision_bandeja", "revisiones_asistidas", ["decision", "created_at"], schema=SCHEMA)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.revision_asistida_inmutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Una revisión asistida no se puede borrar';
            END IF;
            IF OLD.decision IS NOT NULL THEN
                RAISE EXCEPTION 'Una revisión asistida decidida es inmutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER revision_asistida_inmutable BEFORE UPDATE OR DELETE ON {SCHEMA}.revisiones_asistidas
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.revision_asistida_inmutable()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS revision_asistida_inmutable ON {SCHEMA}.revisiones_asistidas")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.revision_asistida_inmutable()")
    op.drop_index("ix_revision_bandeja", table_name="revisiones_asistidas", schema=SCHEMA)
    op.drop_index("uq_revision_abierta", table_name="revisiones_asistidas", schema=SCHEMA)
    op.drop_column("suscripciones", "cotizacion_vigente_hasta", schema=SCHEMA)
    op.drop_column("revisiones_asistidas", "asesor_nombre", schema=SCHEMA)
