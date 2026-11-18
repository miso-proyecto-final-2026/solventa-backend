"""Ofertas de vida hipotecario calculadas con el perfil (HU25).

Tabla de solo inserción, como las de HU23: guarda el precio que se ofreció, la versión de las reglas, el desglose y el
perfil usado (de ahí el linaje de sus fuentes).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "perfilamiento"


def upgrade() -> None:
    op.create_table(
        "ofertas_calculadas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cliente_id", sa.String(64), nullable=False),
        sa.Column(
            "perfil_id",
            sa.String(36),
            sa.ForeignKey(f"{SCHEMA}.perfiles_enriquecidos.id"),
            nullable=False,
        ),
        sa.Column("producto", sa.String(40), nullable=False),
        sa.Column("version_reglas", sa.String(40), nullable=False),
        sa.Column("moneda", sa.String(3), nullable=False),
        sa.Column("suma_asegurada", sa.Numeric(14, 0), nullable=False),
        sa.Column("etapa_de_vida", sa.String(20), nullable=False),
        sa.Column("prima_anual", sa.Numeric(14, 0), nullable=False),
        sa.Column("prima_mensual", sa.Numeric(14, 0), nullable=False),
        sa.Column("desglose", sa.JSON, nullable=False),
        sa.Column("degradado", sa.Boolean, nullable=False),
        sa.Column("consentimiento_id", sa.String(64), nullable=True),
        sa.Column("correlacion_id", sa.String(64), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ofertas_cliente_creado", "ofertas_calculadas", ["cliente_id", "creado_en"], schema=SCHEMA
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_ofertas_calculadas_inmutable
        BEFORE UPDATE OR DELETE ON "{SCHEMA}".ofertas_calculadas
        FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}".solo_insercion()
        """
    )


def downgrade() -> None:
    op.execute(f'DROP TRIGGER IF EXISTS trg_ofertas_calculadas_inmutable ON "{SCHEMA}".ofertas_calculadas')
    op.drop_table("ofertas_calculadas", schema=SCHEMA)
