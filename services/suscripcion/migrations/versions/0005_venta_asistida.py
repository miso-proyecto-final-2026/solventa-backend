"""Venta asistida (HU39): asesor de la saga, evidencia de la firma asistida y auditoría de sus acciones.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def upgrade() -> None:
    op.add_column("suscripciones", sa.Column("asesor_id", sa.String(36), nullable=True), schema=SCHEMA)
    op.create_index("ix_susc_asesor", "suscripciones", ["asesor_id", "created_at"], schema=SCHEMA)
    # Agregar columnas no dispara el disparador de inmutabilidad de `firmas`: es por fila, en UPDATE y DELETE.
    op.add_column(
        "firmas", sa.Column("modalidad", sa.String(20), nullable=False, server_default="CLIENTE"), schema=SCHEMA
    )
    op.add_column("firmas", sa.Column("asesor_id", sa.String(36), nullable=True), schema=SCHEMA)
    op.create_check_constraint(
        "ck_firma_modalidad", "firmas", "modalidad IN ('CLIENTE', 'ASISTIDA_PANTALLA')", schema=SCHEMA
    )
    op.create_table(
        "auditoria_asesor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asesor_id", sa.String(36), nullable=False),
        sa.Column("suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=True),
        sa.Column("cotizacion_id", sa.String(36), nullable=True),
        sa.Column("accion", sa.String(30), nullable=False),
        sa.Column("resultado", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_auditoria_suscripcion", "auditoria_asesor", ["suscripcion_id"], schema=SCHEMA)
    op.create_index("ix_auditoria_asesor", "auditoria_asesor", ["asesor_id", "created_at"], schema=SCHEMA)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.auditoria_asesor_inmutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'La auditoría del asesor es inmutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER auditoria_asesor_inmutable BEFORE UPDATE OR DELETE ON {SCHEMA}.auditoria_asesor
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.auditoria_asesor_inmutable()
        """
    )


def downgrade() -> None:
    op.drop_table("auditoria_asesor", schema=SCHEMA)  # arrastra el disparador y los índices
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.auditoria_asesor_inmutable()")
    op.drop_constraint("ck_firma_modalidad", "firmas", schema=SCHEMA, type_="check")
    op.drop_column("firmas", "asesor_id", schema=SCHEMA)
    op.drop_column("firmas", "modalidad", schema=SCHEMA)
    op.drop_index("ix_susc_asesor", table_name="suscripciones", schema=SCHEMA)
    op.drop_column("suscripciones", "asesor_id", schema=SCHEMA)
