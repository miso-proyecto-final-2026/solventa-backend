"""Piezas de MS Socios por separado: tokens, secretos, almacén, cuota, idempotencia, registro y logs."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from app.socios.almacen import AlmacenMemoria
from app.socios.cuota import LimitadorCuota
from app.socios.errores import ConflictoIdempotencia, ServicioNoDisponible
from app.socios.idempotencia import Idempotencia, huella
from app.socios.registro import RegistroSocios
from app.socios.secretos import hash_secreto, verificar_secreto
from app.socios.tokens import EmisorTokens, ErrorToken, TokenExpiradoError

from app.logs import FiltroDatosPersonales, FormateadorJson, redactar
from tests.dobles import SECRETO_JWT_PRUEBA, SECRETO_PRUEBA, AlmacenCaido

# --- tokens ---------------------------------------------------------------------------------------------------------


def test_un_token_emitido_se_verifica_y_trae_el_socio_y_el_alcance():
    emisor = EmisorTokens(SECRETO_JWT_PRUEBA, 3600)

    emitido = emisor.emitir("socio-a", ["cotizaciones:crear"], datetime.now(UTC))
    claims = emisor.verificar(emitido.access_token)

    assert emitido.expires_in == 3600
    assert claims.client_id == "socio-a"
    assert claims.scope == ("cotizaciones:crear",)


def test_un_token_vencido_lanza_token_expirado():
    emisor = EmisorTokens(SECRETO_JWT_PRUEBA, 60)
    emitido = emisor.emitir("socio-a", [], datetime.now(UTC) - timedelta(hours=1))

    with pytest.raises(TokenExpiradoError):
        emisor.verificar(emitido.access_token)


@pytest.mark.parametrize("token", ["", "no-es-un-jwt", "a.b.c"])
def test_un_token_malformado_es_invalido(token):
    with pytest.raises(ErrorToken):
        EmisorTokens(SECRETO_JWT_PRUEBA).verificar(token)


def test_un_token_de_otro_emisor_es_invalido():
    otro = EmisorTokens("otro-secreto-de-al-menos-treinta-y-dos-caracteres").emitir("socio-a", [], datetime.now(UTC))

    with pytest.raises(ErrorToken):
        EmisorTokens(SECRETO_JWT_PRUEBA).verificar(otro.access_token)


# --- secretos -------------------------------------------------------------------------------------------------------


def test_el_secreto_se_guarda_con_hash_y_se_verifica():
    h = hash_secreto(SECRETO_PRUEBA, 4)

    assert SECRETO_PRUEBA not in h
    assert h.startswith("$2")
    assert verificar_secreto(SECRETO_PRUEBA, h)
    assert not verificar_secreto("otro", h)
    assert not verificar_secreto("", h)
    assert not verificar_secreto(SECRETO_PRUEBA, "no-es-un-hash")


def test_un_secreto_demasiado_largo_se_rechaza_en_vez_de_truncarse():
    with pytest.raises(ValueError):
        hash_secreto("x" * 73, 4)
    assert not verificar_secreto("x" * 73, hash_secreto("x" * 72, 4))


# --- almacén en memoria ---------------------------------------------------------------------------------------------


async def test_el_almacen_en_memoria_vence_las_claves():
    ahora = [100.0]
    almacen = AlmacenMemoria(reloj=lambda: ahora[0])

    assert await almacen.crear_si_no_existe("k", "v", 10)
    assert not await almacen.crear_si_no_existe("k", "otro", 10)
    assert await almacen.incrementar("c", 2) == 1
    assert await almacen.incrementar("c", 2) == 2
    ahora[0] += 11

    assert await almacen.leer("k") is None
    assert await almacen.incrementar("c", 2) == 1
    await almacen.escribir("k", "nuevo", 5)
    assert await almacen.leer("k") == "nuevo"
    await almacen.borrar("k")
    assert await almacen.leer("k") is None
    assert await almacen.disponible()
    await almacen.cerrar()


# --- cuota ----------------------------------------------------------------------------------------------------------


async def test_la_cuota_se_renueva_en_el_siguiente_segundo(almacen):
    ahora = [1000.2]
    limitador = LimitadorCuota(almacen, reloj=lambda: ahora[0])

    assert await limitador.consumir("socio-a", 1) is None
    assert await limitador.consumir("socio-a", 1) == 1
    ahora[0] = 1001.0

    assert await limitador.consumir("socio-a", 1) is None


async def test_sin_almacen_la_cuota_falla_abierta(caplog):
    caplog.set_level(logging.WARNING)

    assert await LimitadorCuota(AlmacenCaido()).consumir("socio-a", 1) is None
    assert "cuota_sin_almacen" in caplog.text


# --- idempotencia ---------------------------------------------------------------------------------------------------


def test_la_huella_no_depende_del_orden_de_las_claves():
    assert huella({"a": 1, "b": {"c": 2, "d": 3}}) == huella({"b": {"d": 3, "c": 2}, "a": 1})
    assert huella({"a": 1}) != huella({"a": 2})


async def test_una_solicitud_en_proceso_con_la_misma_clave_es_un_conflicto(almacen):
    idem = Idempotencia(almacen)

    assert await idem.reservar("socio-a", "k", "h1") is None
    with pytest.raises(ConflictoIdempotencia, match="en proceso"):
        await idem.reservar("socio-a", "k", "h1")


async def test_completada_devuelve_la_respuesta_guardada(almacen):
    idem = Idempotencia(almacen)
    await idem.reservar("socio-a", "k", "h1")
    await idem.completar("socio-a", "k", "h1", 201, {"id": "c-1"})

    guardada = await idem.reservar("socio-a", "k", "h1")

    assert (guardada.status, guardada.cuerpo) == (201, {"id": "c-1"})


async def test_la_clave_se_guarda_con_hash_y_vence(almacen):
    idem = Idempotencia(almacen, ttl_s=60)
    await idem.reservar("socio-a", "clave-visible", "h1")
    await idem.completar("socio-a", "clave-visible", "h1", 201, {"id": "c-1"})

    claves = [c async for c in almacen._r.scan_iter("socios:idem:*")]
    assert len(claves) == 1 and "clave-visible" not in claves[0]
    assert 0 < await almacen._r.ttl(claves[0]) <= 60


async def test_si_la_reserva_vence_entre_el_set_y_el_get_se_reintenta():
    class AlmacenCarrera(AlmacenMemoria):
        def __init__(self):
            super().__init__()
            self.intentos = 0

        async def crear_si_no_existe(self, clave, valor, ttl_s):
            self.intentos += 1
            return self.intentos > 1  # el primer SET NX ve una clave que vence antes del GET

    assert await Idempotencia(AlmacenCarrera()).reservar("socio-a", "k", "h") is None


async def test_si_la_clave_desaparece_dos_veces_es_un_conflicto():
    class AlmacenEsquivo(AlmacenMemoria):
        async def crear_si_no_existe(self, clave, valor, ttl_s):
            return False

    with pytest.raises(ConflictoIdempotencia):
        await Idempotencia(AlmacenEsquivo()).reservar("socio-a", "k", "h")


async def test_sin_almacen_la_idempotencia_falla_cerrada_y_liberar_no_lanza():
    idem = Idempotencia(AlmacenCaido())

    with pytest.raises(ServicioNoDisponible):
        await idem.reservar("socio-a", "k", "h")
    await idem.liberar("socio-a", "k")


# --- registro de socios ---------------------------------------------------------------------------------------------


async def test_el_contrato_se_guarda_en_cache_hasta_su_vencimiento(fabrica, sembrar):
    sembrar(cuota_rps=5)
    ahora = [0.0]
    registro = RegistroSocios(fabrica, ttl_contratos_s=60, monotono=lambda: ahora[0])

    assert (await registro.contrato("socio-a")).cuota_rps == 5
    sembrar(cuota_rps=9)
    assert (await registro.contrato("socio-a")).cuota_rps == 5
    ahora[0] = 61

    assert (await registro.contrato("socio-a")).cuota_rps == 9


async def test_un_contrato_vencido_o_futuro_no_esta_vigente(fabrica, sembrar):
    sembrar()
    futuro = datetime(2025, 1, 1, tzinfo=UTC)

    registro = RegistroSocios(fabrica, ttl_contratos_s=0, reloj=lambda: futuro)

    assert await registro.contrato("socio-a") is None
    assert await registro.contrato("no-existe") is None


async def test_la_base_disponible_se_reporta(fabrica):
    assert await RegistroSocios(fabrica).bd_disponible()


async def test_sin_base_no_esta_disponible():
    def fabrica_rota():
        raise RuntimeError("sin conexión")

    assert not await RegistroSocios(fabrica_rota).bd_disponible()


async def test_autenticar_no_bloquea_el_bucle(fabrica, sembrar):
    sembrar()
    registro = RegistroSocios(fabrica)

    resultados = await asyncio.gather(registro.autenticar("socio-a", SECRETO_PRUEBA), asyncio.sleep(0, "libre"))

    assert resultados[0].habilita("cotizaciones:crear")


# --- logs -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "grant_type=client_credentials&client_id=a&client_secret=SUPERSECRETO",
        '{"client_secret": "SUPERSECRETO"}',
        "Authorization: Bearer SUPERSECRETO",
        "Authorization: Basic SUPERSECRETO",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SUPERSECRETO",
        "access_token=SUPERSECRETO",
    ],
)
def test_las_credenciales_se_enmascaran(texto):
    assert "SUPERSECRETO" not in redactar(texto)


def test_los_logs_salen_en_json_con_traza_y_sin_secretos():
    registro = logging.LogRecord("x", logging.INFO, __file__, 1, "client_secret=%s", ("SUPERSECRETO",), None)
    registro.campos = {"detalle": "Bearer SUPERSECRETO", "n": 1}

    FiltroDatosPersonales().filter(registro)
    linea = json.loads(FormateadorJson().format(registro))

    assert "SUPERSECRETO" not in json.dumps(linea)
    assert {"trace_id", "correlation_id", "timestamp", "level"} <= set(linea)
    assert linea["n"] == 1


def test_las_excepciones_se_redactan():
    try:
        raise ValueError("client_secret=SUPERSECRETO")
    except ValueError:
        import sys

        registro = logging.LogRecord("x", logging.ERROR, __file__, 1, "fallo", None, sys.exc_info())

    FiltroDatosPersonales().filter(registro)

    assert "SUPERSECRETO" not in FormateadorJson().format(registro)
