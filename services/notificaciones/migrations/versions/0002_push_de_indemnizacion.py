"""La bandeja del push admite la indemnización pagada (HU30): lleva la póliza y ya no exige una suscripción.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "notificaciones"


def upgrade() -> None:
    op.add_column("bandeja_push", sa.Column("poliza_id", sa.String(36), nullable=True), schema=SCHEMA)
    op.alter_column("bandeja_push", "suscripcion_id", existing_type=sa.String(36), nullable=True, schema=SCHEMA)


def downgrade() -> None:
    # Las filas sin suscripción (indemnizaciones) no caben en el esquema anterior.
    op.execute(f"DELETE FROM {SCHEMA}.bandeja_push WHERE suscripcion_id IS NULL")
    op.alter_column("bandeja_push", "suscripcion_id", existing_type=sa.String(36), nullable=False, schema=SCHEMA)
    op.drop_column("bandeja_push", "poliza_id", schema=SCHEMA)
