"""Migración y saga contra PostgreSQL real (HU15): índice parcial, carreras entre réplicas y SKIP LOCKED.

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
from app.eventos import PublicadorMemoria
from app.suscripcion import repositorio as repo
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.errores import CotizacionYaUsada
from app.suscripcion.modelos_db import SCHEMA, BaseSuscripcion
from app.suscripcion.reglas import cargar_reglas
from app.suscripcion.relay import RelayEventos
from app.suscripcion.servicio import ServicioSuscripcion
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.dobles import AHORA, CotizacionesFalsas, IdentidadFalsa, PerfilesFalsos
from tests.test_cobros import _resultado
from tests.test_polizas import AlmacenMemoria, _generador

pytestmark = pytest.mark.contenedores

RAIZ = Path(__file__).parents[1]
TABLAS = {"suscripciones", "revisiones_asistidas", "eventos_salida", "eventos_procesados", "polizas", "firmas", "auditoria_asesor"}
REPLICAS = 8
PEDIDO = {"token": "token-1", "cotizacion_id": "cot-1", "metodo_pago": "tok_visa_123", "acepta_terminos": True}


@pytest.fixture
def base_migrada(postgres_url, monkeypatch):
    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    monkeypatch.setenv("SUSCRIPCION_DATABASE_URL", postgres_url)
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(cfg, "head")
    yield engine, cfg
    engine.dispose()


@pytest.fixture
def fabrica_pg(base_migrada):
    engine, _ = base_migrada
    return sessionmaker(bind=repo.crear_engine(str(engine.url.render_as_string(hide_password=False))), expire_on_commit=False)


def _servicio(fabrica, perfiles=None):
    return ServicioSuscripcion(
        fabrica=fabrica,
        identidad=IdentidadFalsa(),
        cotizaciones=CotizacionesFalsas(),
        perfiles=perfiles or PerfilesFalsos(),
        reglas=cargar_reglas(),
        reloj=lambda: AHORA,
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
        assert compare_metadata(contexto, BaseSuscripcion.metadata) == []


def test_la_migracion_es_reversible(base_migrada):
    engine, cfg = base_migrada
    command.downgrade(cfg, "base")
    assert not (TABLAS & set(inspect(engine).get_table_names(schema=SCHEMA)))
    command.upgrade(cfg, "head")
    assert TABLAS <= set(inspect(engine).get_table_names(schema=SCHEMA))


def test_no_hay_claves_foraneas_hacia_otros_esquemas(base_migrada):
    engine, _ = base_migrada
    insp = inspect(engine)
    destinos = {
        fk["referred_schema"] for t in TABLAS for fk in insp.get_foreign_keys(t, schema=SCHEMA)
    }
    assert destinos <= {SCHEMA}


# --- idempotencia y cotización de un solo uso entre réplicas ------------------------------------------------------

def test_la_misma_clave_en_varias_replicas_crea_una_sola_saga(fabrica_pg):
    async def replica(_):
        susc, creada = await _servicio(fabrica_pg).iniciar(clave="reintento", **PEDIDO)
        return susc.id, creada

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert len({id_ for id_, _ in resultados}) == 1
    assert sum(1 for _, creada in resultados if creada) == 1  # solo una réplica creó; las demás repiten
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.suscripciones")).scalar() == 1


def test_claves_distintas_para_la_misma_cotizacion_solo_dejan_pasar_una(fabrica_pg):
    async def replica(i):
        susc, _ = await _servicio(fabrica_pg).iniciar(clave=f"k-{i}", **PEDIDO)
        return susc.id

    resultados = _en_paralelo(REPLICAS, replica)

    ganadoras = [r for r in resultados if isinstance(r, str)]
    perdedoras = [r for r in resultados if isinstance(r, CotizacionYaUsada)]
    assert len(ganadoras) == 1 and len(perdedoras) == REPLICAS - 1, resultados
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.suscripciones")).scalar() == 1


def test_el_indice_parcial_libera_la_cotizacion_al_rechazarse(fabrica_pg):
    async def correr():
        servicio = _servicio(fabrica_pg)
        primera, _ = await servicio.iniciar(clave="k-1", **PEDIDO)
        with fabrica_pg() as s, s.begin():
            repo.obtener(s, primera.id).estado_saga = "RECHAZADA"
        segunda, creada = await servicio.iniciar(clave="k-2", **PEDIDO)
        return primera.id, segunda.id, creada

    primera, segunda, creada = asyncio.run(correr())
    assert creada and primera != segunda


# --- decisión y cola de salida entre réplicas -------------------------------------------------------------------

def test_decidir_en_varias_replicas_deja_una_sola_decision_y_un_solo_evento(fabrica_pg):
    susc, _ = asyncio.run(_servicio(fabrica_pg).iniciar(clave="k", **PEDIDO))

    async def replica(_):
        return await _servicio(fabrica_pg).decidir(susc.id)

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert sum(1 for r in resultados if r is not None) == 1
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.eventos_salida")).scalar() == 1


def test_dos_relevos_no_publican_el_mismo_evento(fabrica_pg):
    servicio = _servicio(fabrica_pg)
    for i in range(6):
        servicio._cotizaciones = CotizacionesFalsas(*[__import__("tests.dobles", fromlist=["x"]).cotizacion(id=f"c{i}")])
        susc, _ = asyncio.run(servicio.iniciar(clave=f"k{i}", **{**PEDIDO, "cotizacion_id": f"c{i}"}))
        asyncio.run(servicio.decidir(susc.id))
    publicadores = [PublicadorMemoria() for _ in range(4)]

    async def relevo(i):
        return await RelayEventos(fabrica_pg, publicadores[i % 4], lote=10).publicar_pendientes()

    _en_paralelo(4, relevo)

    claves = [clave for p in publicadores for _, clave, _ in p.publicados]
    assert len(claves) == 6 and len(set(claves)) == 6  # ninguno repetido, ninguno perdido


# --- póliza (HU20) ----------------------------------------------------------------------------------------------

def _saga_cobrada(fabrica_pg):
    servicio = _servicio(fabrica_pg)
    susc, _ = asyncio.run(servicio.iniciar(clave="k", **PEDIDO))
    asyncio.run(servicio.decidir(susc.id))
    return susc


def test_reentregas_concurrentes_del_mismo_cobro_dejan_una_sola_poliza(fabrica_pg):
    susc = _saga_cobrada(fabrica_pg)

    async def replica(i):
        evento = _resultado(susc.id).model_copy(update={"event_id": f"ev-{i}"})
        with fabrica_pg() as s, s.begin():
            await manejar_resultado_cobro(evento, s)

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.polizas")).scalar() == 1


def test_varias_replicas_del_generador_producen_un_solo_documento_por_poliza(fabrica_pg):
    susc = _saga_cobrada(fabrica_pg)
    asyncio.run(manejar_resultado_cobro_en(fabrica_pg, susc.id))
    almacen = AlmacenMemoria()

    async def replica(_):
        return await _generador(fabrica_pg, almacen).procesar_pendientes()

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert sum(resultados) == 1 and almacen.llamadas == 1  # SKIP LOCKED y la reserva: solo una réplica la toma
    with fabrica_pg() as s:
        fila = s.execute(text(f"SELECT documento_estado, documento_intentos FROM {SCHEMA}.polizas")).one()
    assert tuple(fila) == ("GENERADO", 1)


async def manejar_resultado_cobro_en(fabrica, suscripcion_id):
    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(_resultado(suscripcion_id), s)


def test_la_base_rechaza_un_numero_de_poliza_con_formato_invalido(fabrica_pg):
    susc = _saga_cobrada(fabrica_pg)
    asyncio.run(manejar_resultado_cobro_en(fabrica_pg, susc.id))

    for invalido in ("corto", "I" * 20, "a" * 20):  # largo, letra excluida (I) y minúsculas
        with pytest.raises(IntegrityError), fabrica_pg() as s, s.begin():
            s.execute(text(f"UPDATE {SCHEMA}.polizas SET numero_poliza = :n"), {"n": invalido})


# --- firma electrónica (HU21) ----------------------------------------------------------------------------------

def _lista_para_firmar(fabrica_pg):
    """Una saga con el cobro confirmado y el PDF generado, más el servicio de firma de una réplica."""
    from datetime import timedelta

    from app.suscripcion.firma import ServicioFirma
    from app.suscripcion.proveedor_firma import ProveedorFirmaSimulado

    from tests.test_firma import COBRO, OTP
    from tests.test_polizas import Reloj

    susc = _saga_cobrada(fabrica_pg)
    asyncio.run(manejar_resultado_cobro_en(fabrica_pg, susc.id))
    almacen = AlmacenMemoria()
    asyncio.run(_generador(fabrica_pg, almacen).procesar_pendientes())

    def firma():
        return ServicioFirma(
            fabrica=fabrica_pg, identidad=IdentidadFalsa(), almacen=almacen, proveedor=ProveedorFirmaSimulado(OTP),
            reglas=cargar_reglas(), reloj=Reloj(COBRO + timedelta(hours=1)),
        )

    return susc, firma


def _firmar_en(firma, susc):
    from tests.test_firma import ORIGEN, OTP

    async def firmar():
        documento = await firma.documento_para_firmar(susc.id, "token-1")
        return await firma.firmar(
            susc.id, "token-1", documento_sha256=documento.documento_sha256, documento_revisado=True, codigo=OTP, origen=ORIGEN
        )

    return firmar


def test_varias_replicas_firmando_a_la_vez_dejan_una_firma_y_una_emision(fabrica_pg):
    susc, firma = _lista_para_firmar(fabrica_pg)

    async def replica(_):
        return await _firmar_en(firma(), susc)()

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert sorted(creada for _, creada in resultados) == [False] * (REPLICAS - 1) + [True]
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.firmas")).scalar() == 1
        assert s.execute(text(f"SELECT count(*) FROM {SCHEMA}.eventos_salida WHERE topic = 'solventa.polizas.ciclovida'")).scalar() == 1
        assert s.execute(text(f"SELECT estado_saga FROM {SCHEMA}.suscripciones")).scalar() == "EMITIDA"


def test_la_base_no_deja_corregir_ni_borrar_la_evidencia_de_una_firma(fabrica_pg):
    from sqlalchemy.exc import DBAPIError

    susc, firma = _lista_para_firmar(fabrica_pg)
    asyncio.run(_firmar_en(firma(), susc)())

    with pytest.raises(DBAPIError, match="inmutable"), fabrica_pg() as s, s.begin():
        s.execute(text(f"UPDATE {SCHEMA}.firmas SET canal = 'WEB'"))
    with pytest.raises(DBAPIError, match="inmutable"), fabrica_pg() as s, s.begin():
        s.execute(text(f"DELETE FROM {SCHEMA}.firmas"))
    with fabrica_pg() as s:
        assert s.execute(text(f"SELECT canal FROM {SCHEMA}.firmas")).scalar() == "MOVIL"


def test_la_base_no_deja_corregir_ni_borrar_la_auditoria_del_asesor(fabrica_pg):
    from sqlalchemy.exc import DBAPIError

    with fabrica_pg() as s, s.begin():
        s.execute(
            text(
                f"INSERT INTO {SCHEMA}.auditoria_asesor (asesor_id, accion, resultado, created_at) "
                "VALUES ('ase-1', 'FIRMAR', 'EXITO', now())"
            )
        )

    with pytest.raises(DBAPIError, match="inmutable"), fabrica_pg() as s, s.begin():
        s.execute(text(f"UPDATE {SCHEMA}.auditoria_asesor SET resultado = 'OTRO'"))
    with pytest.raises(DBAPIError, match="inmutable"), fabrica_pg() as s, s.begin():
        s.execute(text(f"DELETE FROM {SCHEMA}.auditoria_asesor"))


def test_la_base_rechaza_una_modalidad_de_firma_invalida(fabrica_pg):
    from sqlalchemy.exc import DBAPIError

    susc, firma = _lista_para_firmar(fabrica_pg)
    asyncio.run(_firmar_en(firma(), susc)())
    with fabrica_pg() as s, s.begin():
        s.execute(text(f"ALTER TABLE {SCHEMA}.firmas DISABLE TRIGGER firmas_inmutables"))
    try:
        with pytest.raises(DBAPIError), fabrica_pg() as s, s.begin():
            s.execute(text(f"UPDATE {SCHEMA}.firmas SET modalidad = 'OTRA'"))
    finally:
        with fabrica_pg() as s, s.begin():
            s.execute(text(f"ALTER TABLE {SCHEMA}.firmas ENABLE TRIGGER firmas_inmutables"))


def test_la_base_rechaza_un_canal_o_un_hash_invalido(fabrica_pg):
    susc, firma = _lista_para_firmar(fabrica_pg)
    asyncio.run(_firmar_en(firma(), susc)())

    for columna, valor in (("canal", "FAX"), ("documento_sha256", "ABC")):
        with fabrica_pg() as s, s.begin():
            s.execute(text(f"ALTER TABLE {SCHEMA}.firmas DISABLE TRIGGER firmas_inmutables"))
        try:
            with pytest.raises(IntegrityError), fabrica_pg() as s, s.begin():
                s.execute(text(f"UPDATE {SCHEMA}.firmas SET {columna} = :v"), {"v": valor})
        finally:
            with fabrica_pg() as s, s.begin():
                s.execute(text(f"ALTER TABLE {SCHEMA}.firmas ENABLE TRIGGER firmas_inmutables"))


def test_dos_barridos_a_la_vez_piden_un_solo_reembolso_de_una_firma_vencida(fabrica_pg):
    from datetime import timedelta

    from app.suscripcion.firma import ServicioFirma
    from app.suscripcion.proveedor_firma import ProveedorFirmaSimulado

    from tests.test_firma import COBRO, OTP
    from tests.test_polizas import Reloj

    _susc, _ = _lista_para_firmar(fabrica_pg)

    async def replica(_):
        servicio = ServicioFirma(
            fabrica=fabrica_pg, identidad=IdentidadFalsa(), almacen=AlmacenMemoria(), proveedor=ProveedorFirmaSimulado(OTP),
            reglas=cargar_reglas(), reloj=Reloj(COBRO + timedelta(hours=25)),
        )
        return await servicio.compensar_vencidas()

    resultados = _en_paralelo(REPLICAS, replica)

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    with fabrica_pg() as s:
        reembolsos = s.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.eventos_salida WHERE payload->>'event_type' = 'reembolso_cobro_solicitado'")
        ).scalar()
    assert reembolsos == 1
