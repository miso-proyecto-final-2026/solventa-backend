"""Esquema de MS Notificaciones (HU22): dispositivos, envíos, bandeja del push simulado e idempotencia.

Revision ID: 0001
Revises:
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "notificaciones"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    op.create_table(
        "dispositivos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("dispositivo_id", sa.String(100), nullable=False),
        sa.Column("idioma", sa.String(5), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cliente_id", "dispositivo_id", name="uq_dispositivo_cliente"),
        sa.CheckConstraint("idioma IN ('es-CO', 'en-US')", name="ck_dispositivo_idioma"),
        schema=SCHEMA,
    )
    op.create_index("ix_dispositivo_cliente", "dispositivos", ["cliente_id", "actualizado_en"], schema=SCHEMA)

    op.create_table(
        "envios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evento_origen_id", sa.String(64), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("canal", sa.String(10), nullable=False),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("idioma", sa.String(5), nullable=False),
        sa.Column("datos", sa.JSON(), nullable=False),
        sa.Column("estado", sa.String(10), nullable=False, server_default="PENDIENTE"),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proximo_intento_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_error", sa.String(100), nullable=True),
        sa.Column("referencia_proveedor", sa.String(200), nullable=True),
        sa.Column("correlacion_id", sa.String(100), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enviado_en", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("evento_origen_id", "canal", name="uq_envio_evento_canal"),
        sa.CheckConstraint("canal IN ('PUSH', 'CORREO')", name="ck_envio_canal"),
        sa.CheckConstraint("estado IN ('PENDIENTE', 'ENVIADO', 'FALLIDO')", name="ck_envio_estado"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_envio_pendientes",
        "envios",
        ["proximo_intento_en"],
        schema=SCHEMA,
        postgresql_where=sa.text("estado = 'PENDIENTE'"),
    )
    op.create_index("ix_envio_fallidos", "envios", ["estado", "creado_en"], schema=SCHEMA)

    op.create_table(
        "bandeja_push",
        sa.Column("envio_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.envios.id"), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("suscripcion_id", sa.String(36), nullable=False),
        sa.Column("titulo", sa.String(120), nullable=False),
        sa.Column("texto", sa.String(300), nullable=False),
        sa.Column("titulo_publico", sa.String(120), nullable=False),
        sa.Column("texto_publico", sa.String(300), nullable=False),
        sa.Column("prioridad", sa.String(10), nullable=False),
        sa.Column("idioma", sa.String(5), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recibida_en", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_bandeja_cliente", "bandeja_push", ["cliente_id", "creado_en"], schema=SCHEMA)

    op.create_table(
        "eventos_procesados",
        sa.Column("consumidor", sa.String(100), primary_key=True),
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("procesado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reentregas", sa.Integer(), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("eventos_procesados", schema=SCHEMA)
    op.drop_table("bandeja_push", schema=SCHEMA)
    op.drop_table("envios", schema=SCHEMA)
    op.drop_table("dispositivos", schema=SCHEMA)
