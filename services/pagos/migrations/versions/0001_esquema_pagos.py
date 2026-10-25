"""Esquema pagos: transacciones, tokens de tarjeta, intentos de cobro, cola de salida, idempotencia y libro del simulador (HU18).

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "pagos"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # Solo el token de la pasarela y los últimos 4: nunca el PAN (EC-S2.1).
    op.create_table(
        "tokens_tarjeta",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(200), nullable=False, unique=True),
        sa.Column("ultimos4", sa.String(4), nullable=True),
        sa.Column("marca", sa.String(20), nullable=True),
        sa.Column("cliente_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    op.create_table(
        "transacciones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        sa.Column("suscripcion_id", sa.String(36), nullable=False),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("monto", sa.Numeric(15, 2), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("periodicidad_pago", sa.String(10), nullable=True),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("token_id", sa.Integer, sa.ForeignKey(f"{SCHEMA}.tokens_tarjeta.id"), nullable=False),
        sa.Column("referencia_pasarela", sa.String(100), nullable=True),
        sa.Column("codigo_rechazo", sa.String(50), nullable=True),
        sa.Column("motivo_rechazo", sa.String(200), nullable=True),
        sa.Column("gracia_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intentos", sa.Integer, nullable=False),
        sa.Column("proximo_intento_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origen", sa.String(10), nullable=False),
        sa.Column("reentregas", sa.Integer, nullable=False),
        sa.Column("correlacion_id", sa.String(100), nullable=True),
        sa.Column("traza_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resuelta_en", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_tx_pendientes",
        "transacciones",
        ["proximo_intento_en"],
        schema=SCHEMA,
        postgresql_where=sa.text("estado = 'PENDIENTE'"),
    )
    op.create_index("ix_tx_suscripcion", "transacciones", ["suscripcion_id"], schema=SCHEMA)

    op.create_table(
        "intentos_cobro",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transaccion_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.transacciones.id"), nullable=False),
        sa.Column("numero", sa.Integer, nullable=False),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("codigo", sa.String(50), nullable=True),
        sa.Column("motivo", sa.String(200), nullable=True),
        sa.Column("referencia_pasarela", sa.String(100), nullable=True),
        sa.Column("duracion_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("transaccion_id", "numero", name="uq_intento_numero"),
        schema=SCHEMA,
    )

    op.create_table(
        "eventos_salida",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("clave", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publicado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intentos", sa.Integer, nullable=False),
        sa.Column("ultimo_error", sa.String(500), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_salida_pendientes",
        "eventos_salida",
        ["id"],
        schema=SCHEMA,
        postgresql_where=sa.text("publicado_en IS NULL"),
    )

    op.create_table(
        "eventos_procesados",
        sa.Column("consumidor", sa.String(100), primary_key=True),
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("procesado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reentregas", sa.Integer, nullable=False),
        schema=SCHEMA,
    )

    # Libro de la pasarela simulada: solo lo usa el adaptador `simulador`.
    op.create_table(
        "simulador_cargos",
        sa.Column("idempotency_key", sa.String(100), primary_key=True),
        sa.Column("referencia", sa.String(100), nullable=False),
        sa.Column("monto", sa.Numeric(15, 2), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("aprobado", sa.Boolean, nullable=False),
        sa.Column("codigo", sa.String(50), nullable=True),
        sa.Column("motivo", sa.String(200), nullable=True),
        sa.Column("solicitudes", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("simulador_cargos", schema=SCHEMA)
    op.drop_table("eventos_procesados", schema=SCHEMA)
    op.drop_table("eventos_salida", schema=SCHEMA)
    op.drop_table("intentos_cobro", schema=SCHEMA)
    op.drop_table("transacciones", schema=SCHEMA)
    op.drop_table("tokens_tarjeta", schema=SCHEMA)
