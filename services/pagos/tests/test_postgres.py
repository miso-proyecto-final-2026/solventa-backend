"""Migración y cobro contra PostgreSQL real (HU18): esquema reversible y carreras entre réplicas.

Cada hilo es una «réplica»: su propio servicio, su propia sesión y su propio bucle de eventos, sin memoria compartida.
La pasarela es el simulador, cuyo libro vive en la base: así se cuentan los cargos reales de todas las réplicas.
Se omite sola si Docker no está disponible.
"""

import asyncio
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app.eventos import RegistroEventos
from app.pagos import repositorio as repo
from app.pagos.modelos_db import SCHEMA, TABLA_EVENTOS_PROCESADOS, BasePagos
from app.pagos.servicio import ComandoCobro, ServicioCobros
from app.pagos.simulador import ModoSimulador, PasarelaSimulada
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from tests.dobles import Reloj, comando, evento

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[1]
TABLAS = {"transacciones", "tokens_tarjeta", "intentos_cobro", "eventos_salida", "eventos_procesados", "simulador_cargos"}
REPLICAS = 8


@pytest.fixture
def base_migrada(postgres_url, monkeypatch):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    monkeypatch.setenv("PAGOS_DATABASE_URL", postgres_url)
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(cfg, "head")
    yield engine, cfg
    engine.dispose()


@pytest.fixture
def fabrica_pg(base_migrada):
    engine, _ = base_migrada
    return sessionmaker(bind=repo.crear_engine(str(engine.url.render_as_string(hide_password=False))), expire_on_commit=False)


def _servicio(fabrica, modo=ModoSimulador.APROBAR, reloj=None):
    return ServicioCobros(
        fabrica=fabrica,
        pasarela=PasarelaSimulada(fabrica, modo=modo),
        periodo_gracia_dias=3,
        timeout_pasarela_s=2,
        reloj=reloj or Reloj(),
    )


def _contar(fabrica, tabla):
    with fabrica() as s:
        return s.execute(text(f"SELECT count(*) FROM {SCHEMA}.{tabla}")).scalar()


def _en_paralelo(n, tarea):
    """Lanza `n` réplicas a la vez; devuelve lo que cada una devolvió o la excepción que lanzó."""
    barrera = threading.Barrier(n)
    resultados = [None] * n

    def replica(i):
        barrera.wait()
        try:
            resultados[i] = asyncio.run(tarea(i))
        except Exception as error:  # noqa: BLE001
            resultados[i] = error

    hilos = [threading.Thread(target=replica, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    return resultados


# --- migración -------------------------------------------------------------------------------------------------

def test_la_migracion_crea_el_esquema_y_sus_tablas(base_migrada):
    engine, _ = base_migrada
    assert TABLAS <= set(inspect(engine).get_table_names(schema=SCHEMA))


def test_la_migracion_coincide_con_los_modelos_sin_deriva(base_migrada):
    engine, _ = base_migrada
    with engine.connect() as conn:
        contexto = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "version_table_schema": SCHEMA,
                "include_name": lambda nombre, tipo, _p: (nombre == SCHEMA if tipo == "schema" else True),
            },
        )
        assert compare_metadata(contexto, BasePagos.metadata) == []


def test_la_migracion_es_reversible(base_migrada):
    engine, cfg = base_migrada
    command.downgrade(cfg, "base")
    assert not (TABLAS & set(inspect(engine).get_table_names(schema=SCHEMA)))
    command.upgrade(cfg, "head")
    assert TABLAS <= set(inspect(engine).get_table_names(schema=SCHEMA))


def test_no_hay_claves_foraneas_hacia_otros_esquemas(base_migrada):
    engine, _ = base_migrada
    insp = inspect(engine)
    destinos = {fk["referred_schema"] for t in TABLAS for fk in insp.get_foreign_keys(t, schema=SCHEMA)}
    assert destinos <= {SCHEMA}


def test_el_monto_es_numeric_15_2(base_migrada):
    engine, _ = base_migrada
    columnas = {c["name"]: c["type"] for c in inspect(engine).get_columns("transacciones", schema=SCHEMA)}
    assert (columnas["monto"].precision, columnas["monto"].scale) == (15, 2)


# --- carrera: el mismo comando desde varias réplicas deja una sola transacción -----------------------------------

def test_el_mismo_comando_en_varias_replicas_deja_una_sola_transaccion_y_un_solo_cargo(fabrica_pg):
    registro = RegistroEventos(TABLA_EVENTOS_PROCESADOS)

    async def replica(i):
        servicio = _servicio(fabrica_pg)
        # Eventos distintos (reentrega de la saga) con la misma idempotency_key, procesados a la vez.
        with fabrica_pg() as s, s.begin():
            assert registro.reservar(s, "solventa-pagos.cobros", f"evento-{i}")
            await servicio.manejar_evento(evento(comando("susc-carrera")), s)
        return await servicio.procesar_pendientes()

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert _contar(fabrica_pg, "transacciones") == 1
    assert _contar(fabrica_pg, "tokens_tarjeta") == 1
    assert _contar(fabrica_pg, "simulador_cargos") == 1
    with fabrica_pg() as s:
        tx = repo.por_clave(s, "cobro_prima:susc-carrera")
        assert tx.estado == "APROBADA" and tx.reentregas == REPLICAS - 1


def test_varias_replicas_procesando_la_misma_transaccion_hacen_un_solo_intento(fabrica_pg):
    servicio = _servicio(fabrica_pg)
    with fabrica_pg() as s, s.begin():
        tx, _ = servicio.registrar(s, ComandoCobro.desde_evento(comando()), origen="kafka")
    tx_id = tx.id

    resultados = _en_paralelo(REPLICAS, lambda _: _servicio(fabrica_pg).procesar(tx_id))

    assert sum(1 for r in resultados if r is not None and not isinstance(r, Exception)) == 1, resultados
    assert _contar(fabrica_pg, "intentos_cobro") == 1
    assert _contar(fabrica_pg, "simulador_cargos") == 1


def test_respuesta_perdida_conciliada_por_otra_replica_no_duplica_el_cargo(fabrica_pg):
    reloj = Reloj()
    perdida = _servicio(fabrica_pg, ModoSimulador.PERDER_RESPUESTA, reloj)

    async def correr():
        with fabrica_pg() as s, s.begin():
            await perdida.manejar_evento(evento(), s)
        await perdida.procesar_pendientes()          # réplica 1: cobra y pierde la respuesta
        reloj.avanzar(10)
        await _servicio(fabrica_pg, reloj=reloj).procesar_pendientes()  # réplica 2: concilia

    asyncio.run(correr())

    with fabrica_pg() as s:
        tx = repo.por_clave(s, "cobro_prima:susc-1")
        assert tx.estado == "APROBADA"
        assert [i.resultado for i in repo.intentos_de(s, tx.id)] == ["SIN_RESPUESTA", "CONCILIADO"]
    assert _contar(fabrica_pg, "simulador_cargos") == 1
