"""Datos de la póliza para la conciliación de Pagos (HU31): cliente, número y monto asegurado.

Los trae `poliza_emitida` y Siniestros los reenvía en el comando de indemnización, porque Pagos no une tablas con
Siniestros (ADR-0001). Son nulos en las pólizas que ya estaban copiadas.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "siniestros"
TABLA = "polizas_parametricas"


def upgrade() -> None:
    op.add_column(TABLA, sa.Column("cliente_id", sa.String(64), nullable=True), schema=SCHEMA)
    op.add_column(TABLA, sa.Column("numero_poliza", sa.String(40), nullable=True), schema=SCHEMA)
    op.add_column(TABLA, sa.Column("monto_asegurado", sa.BigInteger, nullable=True), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column(TABLA, "monto_asegurado", schema=SCHEMA)
    op.drop_column(TABLA, "numero_poliza", schema=SCHEMA)
    op.drop_column(TABLA, "cliente_id", schema=SCHEMA)
