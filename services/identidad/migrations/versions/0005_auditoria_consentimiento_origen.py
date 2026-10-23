"""Origen de cada hecho de consentimiento: IP y user agent (HU07).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22

La tabla `consentimiento_registros` es de solo inserción (trigger de la migración 0001): estas columnas
son nulas en los registros anteriores y se llenan desde la revocación en adelante.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "identidad"
TABLA = "consentimiento_registros"


def upgrade() -> None:
    op.add_column(TABLA, sa.Column("ip_origen", sa.String(45), nullable=True), schema=SCHEMA)
    op.add_column(TABLA, sa.Column("user_agent", sa.String(500), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column(TABLA, "user_agent", schema=SCHEMA)
    op.drop_column(TABLA, "ip_origen", schema=SCHEMA)
