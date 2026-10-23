"""Esquema siniestros: eventos paramétricos, rechazos, publicaciones y procesamiento (HU27).

Las cinco tablas son de solo inserción: son evidencia de auditoría de lo que se recibió, lo que
se rechazó y lo que llegó al final del flujo.

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "siniestros"
TABLAS = (
    "eventos_parametricos",
    "eventos_rechazados",
    "publicaciones_bus",
    "eventos_procesados",
    "reentregas_evento",
)


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    op.create_table(
        "eventos_parametricos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evento_id", sa.String(64), nullable=False),
        sa.Column("fuente", sa.String(64), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("datos", sa.JSON, nullable=False),
        sa.Column("ocurrido_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recibido_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estado_validacion", sa.String(20), nullable=False),
        sa.Column("poliza_id", sa.String(64), nullable=True),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        sa.UniqueConstraint("fuente", "evento_id", name="uq_evento_fuente"),
        schema=SCHEMA,
    )
    op.create_index("ix_eventos_recibido", "eventos_parametricos", ["recibido_en"], schema=SCHEMA)

    op.create_table(
        "eventos_rechazados",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fuente", sa.String(64), nullable=False),
        sa.Column("evento_id", sa.String(64), nullable=True),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("detalles", sa.JSON, nullable=False),
        sa.Column("recibido_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_rechazados_recibido", "eventos_rechazados", ["recibido_en"], schema=SCHEMA)

    op.create_table(
        "publicaciones_bus",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("evento_uuid", sa.String(36), nullable=False),
        sa.Column("topic", sa.String(80), nullable=False),
        sa.Column("particion", sa.Integer, nullable=False),
        sa.Column("posicion", sa.Integer, nullable=False),
        sa.Column("publicado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evento_uuid", name="uq_publicacion_evento"),
        schema=SCHEMA,
    )

    op.create_table(
        "eventos_procesados",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("evento_uuid", sa.String(36), nullable=False),
        sa.Column("consumidor", sa.String(64), nullable=False),
        sa.Column("procesado_en", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evento_uuid", "consumidor", name="uq_procesado_consumidor"),
        schema=SCHEMA,
    )

    op.create_table(
        "reentregas_evento",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("evento_uuid", sa.String(36), nullable=False),
        sa.Column("consumidor", sa.String(64), nullable=False),
        sa.Column("recibido_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    # Solo inserción: ni la aplicación ni una consulta suelta modifican o borran evidencia.
    op.execute(
        f"""
        CREATE FUNCTION "{SCHEMA}".solo_insercion() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% es de solo inserción', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for tabla in TABLAS:
        op.execute(
            f"""
            CREATE TRIGGER trg_{tabla}_inmutable
            BEFORE UPDATE OR DELETE ON "{SCHEMA}".{tabla}
            FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".solo_insercion()
            """
        )


def downgrade() -> None:
    for tabla in TABLAS:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{tabla}_inmutable ON "{SCHEMA}".{tabla}')
    op.execute(f'DROP FUNCTION IF EXISTS "{SCHEMA}".solo_insercion()')
    for tabla in reversed(TABLAS):
        op.drop_table(tabla, schema=SCHEMA)
