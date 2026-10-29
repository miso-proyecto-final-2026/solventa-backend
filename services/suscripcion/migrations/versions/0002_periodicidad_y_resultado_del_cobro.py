"""Periodicidad de pago elegida por el cliente y resultado del paso COBRO informado por MS Pagos (HU18).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "suscripcion"


def _columnas_cobro() -> list[sa.Column]:
    return [
        sa.Column("cobro_estado", sa.String(20), nullable=True),
        sa.Column("cobro_transaccion_id", sa.String(36), nullable=True),
        sa.Column("cobro_referencia", sa.String(100), nullable=True),
        sa.Column("cobro_codigo", sa.String(50), nullable=True),
        sa.Column("cobro_motivo", sa.String(200), nullable=True),
        sa.Column("cobro_gracia_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cobro_resuelto_en", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    # Las sagas existentes se crearon sin elegir: se cobraba la prima anual.
    op.add_column(
        "suscripciones",
        sa.Column("periodicidad_pago", sa.String(10), nullable=False, server_default="ANUAL"),
        schema=SCHEMA,
    )
    for columna in _columnas_cobro():
        op.add_column("suscripciones", columna, schema=SCHEMA)


def downgrade() -> None:
    for columna in reversed(_columnas_cobro()):
        op.drop_column("suscripciones", columna.name, schema=SCHEMA)
    op.drop_column("suscripciones", "periodicidad_pago", schema=SCHEMA)
