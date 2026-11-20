"""Migración reversible contra PostgreSQL real y cuota/idempotencia contra Redis real (HU32).

Se omiten solas si Docker no está disponible (en CI sí corren).
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app.socios import repositorio as repo
from app.socios.almacen import AlmacenRedis
from app.socios.cuota import LimitadorCuota
from app.socios.errores import ConflictoIdempotencia
from app.socios.idempotencia import Idempotencia
from app.socios.modelos_db import SCHEMA, BaseSocios
from app.socios.secretos import hash_secreto
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[1]


@pytest.fixture
def base(postgres_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    alembic = Config(str(RAIZ / "alembic.ini"))
    alembic.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(alembic, "head")
    yield alembic, create_engine(repo.normalizar_url(postgres_url))
    command.downgrade(alembic, "base")


def test_la_migracion_crea_el_esquema_igual_al_modelo_y_es_reversible(base):
    alembic, engine = base

    assert set(inspect(engine).get_table_names(schema=SCHEMA)) >= {"clientes_api", "contratos_api"}
    with engine.connect() as conexion:
        contexto = MigrationContext.configure(
            conexion, opts={"include_schemas": True, "include_name": lambda n, t, _p: n == SCHEMA if t == "schema" else True}
        )
        diferencias = [d for d in compare_metadata(contexto, BaseSocios.metadata) if "alembic_version" not in str(d)]
    assert diferencias == []

    command.downgrade(alembic, "base")
    assert not ({"clientes_api", "contratos_api"} & set(inspect(engine).get_table_names(schema=SCHEMA)))
    command.upgrade(alembic, "head")


def test_contrato_vigente_y_jsonb_en_postgres(base):
    _alembic, engine = base
    fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    with fabrica.begin() as sesion:
        repo.guardar_socio(
            sesion, client_id="pg", nombre_socio="PG", client_secret_hash=hash_secreto("x" * 30, 4),
            version="v1", endpoints=["cotizaciones:crear"], cuota_rps=3, ahora=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with fabrica() as sesion:
        contrato = repo.contrato_vigente(sesion, "pg", "v1", datetime.now(UTC))
        assert contrato.endpoints_habilitados == ["cotizaciones:crear"]
        tipo = sesion.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='socios' AND table_name='contratos_api' AND column_name='endpoints_habilitados'"
            )
        ).scalar_one()
        assert tipo == "jsonb"


async def test_cuota_e_idempotencia_contra_redis_real(redis_url):
    almacen = AlmacenRedis.desde_url(redis_url)
    try:
        await almacen._r.flushdb()
        limitador = LimitadorCuota(almacen, reloj=lambda: 1_900_000_000.0)
        resultados = [await limitador.consumir("socio-r", 2) for _ in range(3)]
        assert resultados == [None, None, 1]

        idem = Idempotencia(almacen)
        # Ocho «réplicas» compiten por la misma clave: solo una la reserva.
        intentos = await asyncio.gather(
            *(idem.reservar("socio-r", "k", "h") for _ in range(8)), return_exceptions=True
        )
        assert sum(r is None for r in intentos) == 1
        assert all(isinstance(r, ConflictoIdempotencia) for r in intentos if r is not None)
        assert await almacen.disponible()
    finally:
        await almacen.cerrar()
