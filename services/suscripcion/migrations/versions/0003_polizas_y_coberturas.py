"""Pólizas con el documento generado (HU20) y copia de las coberturas de la cotización en la saga.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"
# Base32 de Crockford en mayúsculas: sin I, L, O ni U para evitar confusiones al leer o dictar el número.
FORMATO_NUMERO = "numero_poliza ~ '^[0-9A-HJKMNP-TV-Z]{20}$'"


def upgrade() -> None:
    op.add_column("suscripciones", sa.Column("coberturas", sa.JSON(), nullable=True), schema=SCHEMA)
    op.add_column("suscripciones", sa.Column("cotizacion_version_reglas", sa.String(40), nullable=True), schema=SCHEMA)
    op.create_table(
        "polizas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=False, unique=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("numero_poliza", sa.String(30), nullable=False, unique=True),
        sa.Column("estado", sa.String(20), nullable=False, server_default="PENDIENTE_FIRMA"),
        sa.Column("documento_estado", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("documento_intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documento_proximo_intento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("documento_error", sa.String(500), nullable=True),
        sa.Column("documento_s3_key", sa.String(200), nullable=True),
        sa.Column("documento_generado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plantilla_version", sa.String(20), nullable=False),
        sa.Column("vigencia_desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fecha_vencimiento", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fecha_emision", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(FORMATO_NUMERO, name="ck_pol_numero_formato"),
        schema=SCHEMA,
    )
    op.create_index("idx_pol_estado", "polizas", ["estado"], schema=SCHEMA)
    op.create_index("idx_pol_cliente", "polizas", ["cliente_id"], schema=SCHEMA)
    op.create_index(
        "idx_pol_documento_pendiente",
        "polizas",
        ["documento_proximo_intento"],
        schema=SCHEMA,
        postgresql_where=sa.text("documento_estado = 'PENDIENTE'"),
    )


def downgrade() -> None:
    op.drop_table("polizas", schema=SCHEMA)  # arrastra sus índices
    op.drop_column("suscripciones", "cotizacion_version_reglas", schema=SCHEMA)
    op.drop_column("suscripciones", "coberturas", schema=SCHEMA)
