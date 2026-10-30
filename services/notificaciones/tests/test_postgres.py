"""Migración y envíos contra PostgreSQL real (HU22): esquema reversible y carreras entre réplicas.

Cada hilo es una «réplica»: su propio servicio, su propia sesión y su propio bucle de eventos, sin memoria compartida.
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
from app.notificaciones import repositorio as repo
from app.notificaciones.canales import PushSimulado
from app.notificaciones.modelos_db import SCHEMA, BaseNotificaciones
from app.notificaciones.servicio import ServicioNotificaciones
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.dobles import (
    AHORA,
    CertificadosFalsos,
    CorreoFalso,
    IdentidadFalsa,
    Reloj,
    poliza_emitida,
)

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[1]
TABLAS = {"dispositivos", "envios", "bandeja_push", "eventos_procesados"}
REPLICAS = 8


@pytest.fixture
def base_migrada(postgres_url, monkeypatch):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    monkeypatch.setenv("NOTIFICACIONES_DATABASE_URL", postgres_url)
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(cfg, "head")
    yield engine, cfg
    engine.dispose()


@pytest.fixture
def fabrica_pg(base_migrada):
    engine, _ = base_migrada
    return sessionmaker(bind=repo.crear_engine(str(engine.url.render_as_string(hide_password=False))), expire_on_commit=False)


def _servicio(fabrica, correo):
    return ServicioNotificaciones(
        fabrica=fabrica,
        identidad=IdentidadFalsa(),
        certificados=CertificadosFalsos(),
        correo=correo,
        push=PushSimulado(fabrica, reloj=Reloj()),
        url_publica="https://borde.example",
        reloj=Reloj(),
    )


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


def _contar(fabrica, tabla, donde=""):
    with fabrica() as s:
        return s.execute(text(f"SELECT count(*) FROM {SCHEMA}.{tabla} {donde}")).scalar()


# --- migración -------------------------------------------------------------------------------------------------------

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
        assert compare_metadata(contexto, BaseNotificaciones.metadata) == []


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


def test_la_base_rechaza_un_canal_un_estado_o_un_idioma_invalidos(fabrica_pg):
    base = (
        f"INSERT INTO {SCHEMA}.envios (id, evento_origen_id, tipo, canal, cliente_id, idioma, datos, estado, intentos, creado_en) "
        "VALUES (:id, :ev, 'POLIZA_EMITIDA', :canal, 'cli-1', 'es-CO', '{}', :estado, 0, now())"
    )
    for canal, estado in (("SMS", "PENDIENTE"), ("PUSH", "ENVIANDO")):
        with pytest.raises(IntegrityError), fabrica_pg() as s, s.begin():
            s.execute(text(base), {"id": canal + estado, "ev": "ev-1", "canal": canal, "estado": estado})
    with pytest.raises(IntegrityError), fabrica_pg() as s, s.begin():
        s.execute(
            text(
                f"INSERT INTO {SCHEMA}.dispositivos (id, cliente_id, dispositivo_id, idioma, creado_en, actualizado_en) "
                "VALUES ('d1', 'cli-1', 'telefono-123456', 'fr-FR', now(), now())"
            )
        )


def test_la_base_no_deja_repetir_un_envio_del_mismo_evento_y_canal(fabrica_pg):
    envio = (
        f"INSERT INTO {SCHEMA}.envios (id, evento_origen_id, tipo, canal, cliente_id, idioma, datos, estado, intentos, creado_en) "
        "VALUES (:id, 'ev-1', 'POLIZA_EMITIDA', 'PUSH', 'cli-1', 'es-CO', '{}', 'PENDIENTE', 0, now())"
    )
    with fabrica_pg() as s, s.begin():
        s.execute(text(envio), {"id": "e1"})
    with pytest.raises(IntegrityError), fabrica_pg() as s, s.begin():
        s.execute(text(envio), {"id": "e2"})


# --- carreras entre réplicas -----------------------------------------------------------------------------------------

def test_la_misma_entrega_de_varias_replicas_deja_un_solo_par_de_envios(fabrica_pg):
    """Sin el registro de eventos procesados de por medio: la restricción única sola evita los duplicados."""
    evento = poliza_emitida()

    async def replica(_):
        servicio = _servicio(fabrica_pg, CorreoFalso())
        with fabrica_pg() as s, s.begin():
            await servicio.manejar_evento(evento, s)

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert _contar(fabrica_pg, "envios") == 2


def test_varias_replicas_entregan_cada_envio_una_sola_vez(fabrica_pg):
    correo = CorreoFalso()
    eventos = [poliza_emitida(poliza_id=f"pol-{i}") for i in range(12)]

    async def sembrar():
        servicio = _servicio(fabrica_pg, correo)
        for e in eventos:
            with fabrica_pg() as s, s.begin():
                await servicio.manejar_evento(e, s)

    asyncio.run(sembrar())
    assert _contar(fabrica_pg, "envios") == 24

    async def replica(_):
        servicio = _servicio(fabrica_pg, correo)
        total = 0
        for _vuelta in range(6):
            total += await servicio.procesar_pendientes(limite=5)
        return total

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert sum(resultados) == 24  # SKIP LOCKED y la reserva: ningún envío lo toma más de una réplica
    assert len(correo.enviados) == 12  # un correo por póliza
    assert _contar(fabrica_pg, "bandeja_push") == 12
    assert _contar(fabrica_pg, "envios", "WHERE estado = 'ENVIADO' AND intentos = 1") == 24


def test_dos_registros_del_mismo_dispositivo_a_la_vez_dejan_uno(fabrica_pg):
    async def replica(i):
        with fabrica_pg() as s, s.begin():
            repo.registrar_dispositivo(s, "cli-1", "telefono-123456", "en-US" if i % 2 else "es-CO", AHORA)

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert _contar(fabrica_pg, "dispositivos") == 1
