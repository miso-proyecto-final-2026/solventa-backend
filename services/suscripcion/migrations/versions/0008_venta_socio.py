"""Venta embebida del socio (HU33): socio de la saga, firma con modalidad SOCIO y auditoría de sus acciones.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def upgrade() -> None:
    op.add_column("suscripciones", sa.Column("socio_client_id", sa.String(64), nullable=True), schema=SCHEMA)
    op.create_index("ix_susc_socio", "suscripciones", ["socio_client_id", "created_at"], schema=SCHEMA)
    # Agregar columnas no dispara el disparador de inmutabilidad de `firmas`: es por fila, en UPDATE y DELETE.
    op.add_column("firmas", sa.Column("socio_client_id", sa.String(64), nullable=True), schema=SCHEMA)
    op.drop_constraint("ck_firma_modalidad", "firmas", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_firma_modalidad",
        "firmas",
        "modalidad IN ('CLIENTE', 'ASISTIDA_PANTALLA', 'ASISTIDA_ENLACE', 'SOCIO')",
        schema=SCHEMA,
    )
    op.create_table(
        "auditoria_socio",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("socio_client_id", sa.String(64), nullable=False),
        sa.Column("suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=True),
        sa.Column("cotizacion_id", sa.String(36), nullable=True),
        sa.Column("accion", sa.String(30), nullable=False),
        sa.Column("resultado", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_auditoria_socio_suscripcion", "auditoria_socio", ["suscripcion_id"], schema=SCHEMA)
    op.create_index("ix_auditoria_socio", "auditoria_socio", ["socio_client_id", "created_at"], schema=SCHEMA)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.auditoria_socio_inmutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'La auditoría del socio es inmutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER auditoria_socio_inmutable BEFORE UPDATE OR DELETE ON {SCHEMA}.auditoria_socio
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.auditoria_socio_inmutable()
        """
    )


def downgrade() -> None:
    op.drop_table("auditoria_socio", schema=SCHEMA)  # arrastra el disparador y los índices
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.auditoria_socio_inmutable()")
    # La evidencia es inmutable: con firmas SOCIO ya guardadas, la restricción anterior no se puede restaurar (como en 0006).
    op.drop_constraint("ck_firma_modalidad", "firmas", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_firma_modalidad", "firmas", "modalidad IN ('CLIENTE', 'ASISTIDA_PANTALLA', 'ASISTIDA_ENLACE')", schema=SCHEMA
    )
    op.drop_column("firmas", "socio_client_id", schema=SCHEMA)
    op.drop_index("ix_susc_socio", table_name="suscripciones", schema=SCHEMA)
    op.drop_column("suscripciones", "socio_client_id", schema=SCHEMA)
