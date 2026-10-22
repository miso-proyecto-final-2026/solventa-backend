"""Esquema perfilamiento: perfiles enriquecidos, linaje e intentos bloqueados (HU23).

Las tres tablas son de solo inserción: el perfil y el linaje son evidencia de auditoría y un nuevo
enriquecimiento crea otro perfil.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "perfilamiento"
TABLAS = ("perfiles_enriquecidos", "linaje_fuentes", "intentos_bloqueados")


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    op.create_table(
        "perfiles_enriquecidos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("perfil", sa.JSON, nullable=False),
        sa.Column("completitud", sa.Float, nullable=False),
        sa.Column("degradado", sa.Boolean, nullable=False),
        sa.Column("consentimiento_id", sa.String(64), nullable=True),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_perfiles_cliente_creado",
        "perfiles_enriquecidos",
        ["cliente_id", "creado_en"],
        schema=SCHEMA,
    )

    op.create_table(
        "linaje_fuentes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "perfil_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.perfiles_enriquecidos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fuente", sa.String(30), nullable=False),
        sa.Column("campos", sa.JSON, nullable=False),
        sa.Column("obtenido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ttl_s", sa.Integer, nullable=True),
        sa.Column("hit_cache", sa.Boolean, nullable=False),
        sa.Column("degradado", sa.Boolean, nullable=False),
        sa.Column("por_defecto", sa.Boolean, nullable=False),
        sa.Column("consentimiento_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_linaje_perfil_id", "linaje_fuentes", ["perfil_id"], schema=SCHEMA)

    op.create_table(
        "intentos_bloqueados",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column("operacion", sa.String(30), nullable=False),
        sa.Column("origen", sa.String(64), nullable=False),
        sa.Column("estado_consentimiento", sa.String(20), nullable=False),
        sa.Column("ocurrido_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_intentos_cliente", "intentos_bloqueados", ["cliente_id"], schema=SCHEMA)

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
    op.drop_table("intentos_bloqueados", schema=SCHEMA)
    op.drop_table("linaje_fuentes", schema=SCHEMA)
    op.drop_table("perfiles_enriquecidos", schema=SCHEMA)
