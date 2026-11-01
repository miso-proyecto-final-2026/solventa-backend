"""Firma por enlace (HU39): enlaces enviados por correo y la modalidad de firma que los usa.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def upgrade() -> None:
    op.drop_constraint("ck_firma_modalidad", "firmas", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_firma_modalidad", "firmas", "modalidad IN ('CLIENTE', 'ASISTIDA_PANTALLA', 'ASISTIDA_ENLACE')", schema=SCHEMA
    )
    op.create_table(
        "enlaces_firma",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=False),
        sa.Column("asesor_id", sa.String(36), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("vence_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_en", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_enlace_suscripcion", "enlaces_firma", ["suscripcion_id", "creado_en"], schema=SCHEMA)
    op.create_index(
        "uq_enlace_activo",
        "enlaces_firma",
        ["suscripcion_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("estado = 'ACTIVO'"),
    )


def downgrade() -> None:
    op.drop_table("enlaces_firma", schema=SCHEMA)
    # La evidencia es inmutable: con firmas ASISTIDA_ENLACE ya guardadas, la restricción anterior no se puede restaurar.
    op.drop_constraint("ck_firma_modalidad", "firmas", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_firma_modalidad", "firmas", "modalidad IN ('CLIENTE', 'ASISTIDA_PANTALLA')", schema=SCHEMA
    )
