"""Firma electrónica de la póliza (HU21): evidencia inmutable, vencimiento y compensación de la saga.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def upgrade() -> None:
    op.add_column("polizas", sa.Column("documento_sha256", sa.String(64), nullable=True), schema=SCHEMA)
    op.add_column("suscripciones", sa.Column("firma_vence_en", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.add_column(
        "suscripciones",
        sa.Column("firma_intentos_fallidos", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.add_column("suscripciones", sa.Column("compensacion_estado", sa.String(20), nullable=True), schema=SCHEMA)
    op.add_column("suscripciones", sa.Column("compensacion_motivo", sa.String(50), nullable=True), schema=SCHEMA)
    op.add_column(
        "suscripciones", sa.Column("compensacion_solicitada_en", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA
    )
    op.create_table(
        "firmas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suscripcion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.suscripciones.id"), nullable=False, unique=True),
        sa.Column("poliza_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.polizas.id"), nullable=False, unique=True),
        sa.Column("firmante_id", sa.String(64), nullable=False),
        sa.Column("documento_sha256", sa.String(64), nullable=False),
        sa.Column("firmada_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canal", sa.String(10), nullable=False),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("dispositivo", sa.String(200), nullable=True),
        sa.Column("metodo_autenticacion", sa.String(20), nullable=False),
        sa.Column("proveedor", sa.String(30), nullable=False),
        sa.Column("referencia_proveedor", sa.String(100), nullable=True),
        sa.Column("copia_firmada_s3_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("canal IN ('MOVIL', 'WEB')", name="ck_firma_canal"),
        sa.CheckConstraint("documento_sha256 ~ '^[0-9a-f]{64}$'", name="ck_firma_hash"),
        schema=SCHEMA,
    )
    # La evidencia no se corrige ni se borra, ni siquiera con acceso directo a la base (criterio 3: inmutable).
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.firmas_inmutables() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'La evidencia de una firma es inmutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER firmas_inmutables BEFORE UPDATE OR DELETE ON {SCHEMA}.firmas
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.firmas_inmutables()
        """
    )


def downgrade() -> None:
    op.drop_table("firmas", schema=SCHEMA)  # arrastra el disparador
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.firmas_inmutables()")
    for columna in (
        "compensacion_solicitada_en",
        "compensacion_motivo",
        "compensacion_estado",
        "firma_intentos_fallidos",
        "firma_vence_en",
    ):
        op.drop_column("suscripciones", columna, schema=SCHEMA)
    op.drop_column("polizas", "documento_sha256", schema=SCHEMA)
