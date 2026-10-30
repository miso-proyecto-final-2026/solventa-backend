"""Gestión de cobros fallidos (HU19): casos y notas de seguimiento.

Los cobros ya rechazados al aplicar la migración abren su caso con la misma fecha límite de gracia que ya tenían.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "pagos"


def upgrade() -> None:
    op.create_table(
        "cobros_fallidos",
        sa.Column("id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.transacciones.id"), primary_key=True),
        sa.Column("suscripcion_id", sa.String(36), nullable=False),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("monto", sa.Numeric(15, 2), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("token_id", sa.Integer, sa.ForeignKey(f"{SCHEMA}.tokens_tarjeta.id"), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("gracia_hasta", sa.DateTime(timezone=True), nullable=False),
        sa.Column("codigo_rechazo", sa.String(50), nullable=True),
        sa.Column("motivo_rechazo", sa.String(200), nullable=True),
        sa.Column("ultimo_intento_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reintentos", sa.Integer, nullable=False),
        sa.Column("transaccion_vigente_id", sa.String(36), nullable=True),
        sa.Column("clave_solicitud", sa.String(100), nullable=True),
        sa.Column("operador_notificado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resuelto_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_caso_estado_gracia", "cobros_fallidos", ["estado", "gracia_hasta"], schema=SCHEMA)
    op.create_index("ix_caso_suscripcion", "cobros_fallidos", ["suscripcion_id"], schema=SCHEMA)

    op.create_table(
        "notas_cobro_fallido",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("caso_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.cobros_fallidos.id"), nullable=False),
        sa.Column("autor_id", sa.String(64), nullable=False),
        sa.Column("autor_nombre", sa.String(200), nullable=False),
        sa.Column("autor_rol", sa.String(20), nullable=False),
        sa.Column("texto", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_nota_caso", "notas_cobro_fallido", ["caso_id", "created_at"], schema=SCHEMA)

    # Cobros rechazados antes de HU19: abren su caso (estado PENDIENTE; el detector los vence si la gracia ya pasó).
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.cobros_fallidos (
            id, suscripcion_id, cliente_id, monto, moneda, token_id, estado, gracia_hasta, codigo_rechazo,
            motivo_rechazo, ultimo_intento_en, reintentos, created_at, updated_at
        )
        SELECT id, suscripcion_id, cliente_id, monto, moneda, token_id, 'PENDIENTE', gracia_hasta, codigo_rechazo,
               motivo_rechazo, COALESCE(resuelta_en, updated_at), 0, updated_at, updated_at
        FROM {SCHEMA}.transacciones
        WHERE estado = 'RECHAZADA' AND tipo = 'COBRO_PRIMA' AND gracia_hasta IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_nota_caso", table_name="notas_cobro_fallido", schema=SCHEMA)
    op.drop_table("notas_cobro_fallido", schema=SCHEMA)
    op.drop_index("ix_caso_suscripcion", table_name="cobros_fallidos", schema=SCHEMA)
    op.drop_index("ix_caso_estado_gracia", table_name="cobros_fallidos", schema=SCHEMA)
    op.drop_table("cobros_fallidos", schema=SCHEMA)
