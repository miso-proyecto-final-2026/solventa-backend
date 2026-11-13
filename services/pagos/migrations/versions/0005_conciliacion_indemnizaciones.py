"""Registro completo y conciliación de las indemnizaciones (HU31).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "pagos"
TABLA = "indemnizaciones"
COLUMNAS = [
    sa.Column("tipo_evento", sa.String(40), nullable=True),
    sa.Column("evento_ocurrido_en", sa.DateTime(timezone=True), nullable=True),
    sa.Column("ubicacion", sa.String(64), nullable=True),
    sa.Column("datos_evento", sa.JSON, nullable=True),
    sa.Column("datos_poliza", sa.JSON, nullable=True),
    sa.Column("validacion", sa.JSON, nullable=True),
    sa.Column("estado_conciliacion", sa.String(20), nullable=False, server_default="PENDIENTE"),
    sa.Column("conciliado_por", sa.String(64), nullable=True),
    sa.Column("conciliado_por_nombre", sa.String(100), nullable=True),
    sa.Column("conciliado_en", sa.DateTime(timezone=True), nullable=True),
    sa.Column("diferencia", sa.JSON, nullable=True),
    sa.Column("escalada_en", sa.DateTime(timezone=True), nullable=True),
    sa.Column("escalada_por", sa.String(64), nullable=True),
    sa.Column("nota_escalamiento", sa.String(500), nullable=True),
]


def upgrade() -> None:
    for columna in COLUMNAS:
        op.add_column(TABLA, columna, schema=SCHEMA)
    op.create_index(
        "ix_indemnizaciones_conciliacion", TABLA, ["estado_conciliacion", "tipo_evento"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_indemnizaciones_conciliacion", table_name=TABLA, schema=SCHEMA)
    for columna in reversed(COLUMNAS):
        op.drop_column(TABLA, columna.name, schema=SCHEMA)
