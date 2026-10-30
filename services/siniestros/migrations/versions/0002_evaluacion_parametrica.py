"""Evaluación de la condición paramétrica: copia de pólizas y resultados por evento–póliza (HU28).

`polizas_parametricas` se actualiza (la alimentan los eventos del ciclo de vida de la póliza).
`evaluaciones_parametricas` es de solo inserción: cada decisión queda como evidencia de auditoría.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "siniestros"


def upgrade() -> None:
    op.create_table(
        "polizas_parametricas",
        sa.Column("poliza_id", sa.String(64), primary_key=True),
        sa.Column("producto", sa.String(50), nullable=False),
        sa.Column("zona", sa.String(64), nullable=True),
        sa.Column("vigencia_desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fecha_vencimiento", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actualizada_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_polizas_param_producto", "polizas_parametricas", ["producto", "poliza_id"], schema=SCHEMA
    )

    op.create_table(
        "evaluaciones_parametricas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evento_uuid", sa.String(36), nullable=False),
        sa.Column("poliza_id", sa.String(64), nullable=False),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("motivo", sa.String(80), nullable=False),
        sa.Column("valores", sa.JSON, nullable=False),
        sa.Column("version_reglas", sa.String(20), nullable=False),
        sa.Column("comando_id", sa.String(64), nullable=True),
        sa.Column("evaluado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evento_uuid", "poliza_id", name="uq_evaluacion_evento_poliza"),
        sa.CheckConstraint(
            "resultado IN ('CORRESPONDE_PAGO', 'NO_CORRESPONDE')", name="ck_evaluacion_resultado"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evaluaciones_poliza", "evaluaciones_parametricas", ["poliza_id", "evaluado_en"], schema=SCHEMA
    )

    # `solo_insercion()` la creó la migración 0001.
    op.execute(
        f"""
        CREATE TRIGGER trg_evaluaciones_parametricas_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".evaluaciones_parametricas
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".solo_insercion()
        """
    )


def downgrade() -> None:
    op.execute(
        f'DROP TRIGGER IF EXISTS trg_evaluaciones_parametricas_inmutable ON "{SCHEMA}".evaluaciones_parametricas'
    )
    op.drop_table("evaluaciones_parametricas", schema=SCHEMA)
    op.drop_table("polizas_parametricas", schema=SCHEMA)
