"""Esquema identidad: clientes, sesiones y consentimientos (HU01, HU06).

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "identidad"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    op.create_table(
        "clientes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("apellido", sa.String(200), nullable=False),
        sa.Column("tipo_documento", sa.String(20), nullable=False),
        sa.Column("numero_documento", sa.String(20), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("celular", sa.String(20), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("estado_kyc", sa.String(20), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_identidad_clientes_numero_documento", "clientes", ["numero_documento"], unique=True, schema=SCHEMA)
    op.create_index("ix_identidad_clientes_email", "clientes", ["email"], unique=True, schema=SCHEMA)

    # Los clientes registrados antes de separar el esquema vivían en public.clientes. La copia es de mejor
    # esfuerzo: en staging esa tabla pertenece al rol del servicio anterior y este rol puede no tener
    # permiso de lectura; en ese caso se omite en lugar de bloquear el despliegue.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.clientes') IS NOT NULL THEN
                BEGIN
                    INSERT INTO "{SCHEMA}".clientes
                        (id, nombre, apellido, tipo_documento, numero_documento, email, celular,
                         password_hash, estado_kyc, creado_en, actualizado_en)
                    SELECT id, nombre, apellido, tipo_documento, numero_documento, email, celular,
                           password_hash, estado_kyc, creado_en, actualizado_en
                    FROM public.clientes
                    ON CONFLICT DO NOTHING;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'Sin permiso sobre public.clientes: no se copian los clientes previos';
                END;
            END IF;
        END $$;
        """
    )

    op.create_table(
        "sesiones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.clientes.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_identidad_sesiones_cliente_id", "sesiones", ["cliente_id"], schema=SCHEMA)

    op.create_table(
        "consentimientos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.clientes.id"), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("version_texto", sa.String(20), nullable=False),
        sa.Column("fecha_otorgamiento", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fecha_revocacion", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("cliente_id", "tipo", name="uq_consentimiento_cliente_tipo"),
        schema=SCHEMA,
    )

    op.create_table(
        "consentimiento_registros",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.clientes.id"), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("evento", sa.String(20), nullable=False),
        sa.Column("version_texto", sa.String(20), nullable=False),
        sa.Column("canal", sa.String(10), nullable=False),
        sa.Column("fecha", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_registro_cliente_tipo", "consentimiento_registros", ["cliente_id", "tipo"], schema=SCHEMA
    )

    # Inmutabilidad a nivel de base de datos: el registro solo admite inserciones.
    op.execute(
        f"""
        CREATE FUNCTION "{SCHEMA}".consentimiento_registro_inmutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'consentimiento_registros es inmutable: solo admite inserciones';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER consentimiento_registro_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".consentimiento_registros
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".consentimiento_registro_inmutable();
        """
    )


def downgrade() -> None:
    for tabla in ("consentimiento_registros", "consentimientos", "sesiones", "clientes"):
        op.drop_table(tabla, schema=SCHEMA)
    op.execute(f'DROP FUNCTION IF EXISTS "{SCHEMA}".consentimiento_registro_inmutable()')
