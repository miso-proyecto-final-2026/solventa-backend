"""Rol OPERADOR para los usuarios internos (HU19): los roles válidos quedan fijados en la base.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28

Hasta ahora `rol` era texto libre y solo existía ASESOR. El operador gestiona los cobros fallidos y recibe el aviso de
gracia vencida. La restricción falla si hubiera un rol distinto: se revisa antes de aplicar.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = "identidad"
RESTRICCION = "ck_usuarios_internos_rol"


def upgrade() -> None:
    op.create_check_constraint(RESTRICCION, "usuarios_internos", "rol IN ('ASESOR', 'OPERADOR')", schema=SCHEMA)


def downgrade() -> None:
    op.drop_constraint(RESTRICCION, "usuarios_internos", type_="check", schema=SCHEMA)
