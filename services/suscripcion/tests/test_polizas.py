"""Póliza de la suscripción (HU20): registro idempotente, número único, documento fiel, reintentos y emisión."""

import logging
import re
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.documento import cargar_plantilla
from app.suscripcion.errores import PolizaNoLista
from app.suscripcion.modelos_db import PolizaDB, SuscripcionDB
from app.suscripcion.polizas import (
    ALFABETO,
    GeneradorDocumentos,
    NumeroPolizaNoGenerado,
    clave_documento,
    emitir_poliza,
    generar_numero,
    registrar_poliza,
    sumar_meses,
)
from pypdf import PdfReader

from tests.dobles import AHORA, CotizacionesFalsas, IdentidadFalsa, cotizacion
from tests.test_cobros import _resultado
from tests.test_servicio import _iniciar, _salidas

FORMATO = re.compile(r"^[0-9A-HJKMNP-TV-Z]{20}$")


class AlmacenMemoria:
    def __init__(self, fallos=0):
        self.objetos: dict[str, tuple[bytes, str]] = {}
        self.fallos = fallos
        self.llamadas = 0

    def guardar(self, clave, contenido, content_type):
        self.llamadas += 1
        if self.fallos > 0:
            self.fallos -= 1
            raise OSError("almacén no disponible")
        self.objetos[clave] = (contenido, content_type)


class Reloj:
    def __init__(self, inicio=AHORA):
        self.ahora = inicio

    def __call__(self):
        return self.ahora

    def avanzar(self, segundos):
        self.ahora += timedelta(seconds=segundos)


def _texto(pdf: bytes) -> str:
    paginas = PdfReader(BytesIO(pdf)).pages
    return re.sub(r"\s+", " ", " ".join(p.extract_text() for p in paginas))


async def _cobrada(armar, fabrica, producto="microseguro_vida", **cambios):
    servicio = armar(cotizaciones=CotizacionesFalsas(cotizacion(producto=producto)))
    susc, _ = await _iniciar(servicio, **cambios)
    await servicio.decidir(susc.id)
    await _confirmar(fabrica, susc.id)
    return servicio, susc


async def _confirmar(fabrica, suscripcion_id, **extra):
    evento = _resultado(suscripcion_id, **extra)
    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(evento, s)


def _poliza(fabrica, suscripcion_id) -> PolizaDB:
    with fabrica() as s:
        return repo.poliza_de_suscripcion(s, suscripcion_id)


def _generador(fabrica, almacen, reloj=None, identidad=None, cotizaciones=None, **cambios):
    return GeneradorDocumentos(
        fabrica=fabrica,
        almacen=almacen,
        identidad=identidad or IdentidadFalsa(),
        cotizaciones=cotizaciones or CotizacionesFalsas(),
        plantilla=cargar_plantilla(),
        reloj=reloj or Reloj(),
        **cambios,
    )


# --- número de póliza (criterio 2) ---------------------------------------------------------------------------------

def test_el_numero_tiene_20_caracteres_del_alfabeto_de_crockford():
    numeros = {generar_numero() for _ in range(500)}
    assert len(numeros) == 500
    assert all(FORMATO.match(n) for n in numeros)
    assert not set("ILOU") & set(ALFABETO)


async def test_ante_una_colision_se_genera_otro_numero(armar, fabrica):
    _, primera = await _cobrada(armar, fabrica)
    repetido = _poliza(fabrica, primera.id).numero_poliza
    segunda, _ = await _iniciar(armar(cotizaciones=CotizacionesFalsas(cotizacion(id="cot-2"))), cotizacion_id="cot-2", clave="k-2")
    ofrecidos = iter([repetido, repetido, "0" * 20])

    with fabrica() as s, s.begin():
        poliza = registrar_poliza(s, s.get(SuscripcionDB, segunda.id), AHORA, generar=lambda: next(ofrecidos))

    assert poliza.numero_poliza == "0" * 20


async def test_tras_tres_colisiones_seguidas_falla_sin_asignar_numero(armar, fabrica):
    _, primera = await _cobrada(armar, fabrica)
    repetido = _poliza(fabrica, primera.id).numero_poliza
    segunda, _ = await _iniciar(armar(cotizaciones=CotizacionesFalsas(cotizacion(id="cot-2"))), cotizacion_id="cot-2", clave="k-2")

    with pytest.raises(NumeroPolizaNoGenerado), fabrica() as s, s.begin():
        registrar_poliza(s, s.get(SuscripcionDB, segunda.id), AHORA, generar=lambda: repetido)

    assert _poliza(fabrica, segunda.id) is None


# --- idempotencia (criterio 3) -------------------------------------------------------------------------------------

async def test_el_cobro_confirmado_registra_la_poliza_pendiente_de_firma(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    poliza = _poliza(fabrica, susc.id)

    assert FORMATO.match(poliza.numero_poliza)
    assert (poliza.estado, poliza.documento_estado, poliza.documento_intentos) == ("PENDIENTE_FIRMA", "PENDIENTE", 0)
    assert (poliza.cliente_id, poliza.plantilla_version, poliza.fecha_emision) == ("cli-1", "v1", None)
    with fabrica() as s:
        saga = repo.obtener(s, susc.id)
    assert (saga.estado_saga, saga.paso_actual) == ("COBRO_REALIZADO", "FIRMA")


async def test_reentregar_el_cobro_confirmado_no_crea_otra_poliza_ni_cambia_el_numero(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    numero = _poliza(fabrica, susc.id).numero_poliza

    await _confirmar(fabrica, susc.id)
    await _confirmar(fabrica, susc.id)

    with fabrica() as s:
        assert s.query(PolizaDB).count() == 1
    assert _poliza(fabrica, susc.id).numero_poliza == numero


async def test_registrar_dos_veces_devuelve_la_misma_poliza(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    with fabrica() as s, s.begin():
        de_nuevo = registrar_poliza(s, s.get(SuscripcionDB, susc.id), AHORA + timedelta(days=1))

    assert de_nuevo.numero_poliza == _poliza(fabrica, susc.id).numero_poliza


async def test_un_cobro_rechazado_no_registra_poliza(armar, fabrica):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)

    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(_resultado(susc.id, "cobro_prima_rechazado", codigo="FONDOS", motivo="x"), s)

    assert _poliza(fabrica, susc.id) is None


# --- vigencia ------------------------------------------------------------------------------------------------------

async def test_la_vigencia_son_12_meses_desde_el_cobro(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    poliza = _poliza(fabrica, susc.id)

    assert repo.utc(poliza.vigencia_desde) == datetime(2026, 10, 25, 12, 0, 5, tzinfo=UTC)
    assert repo.utc(poliza.fecha_vencimiento) == datetime(2027, 10, 25, 12, 0, 5, tzinfo=UTC)


def test_sumar_meses_respeta_el_fin_de_mes():
    assert sumar_meses(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert sumar_meses(datetime(2027, 12, 15, tzinfo=UTC), 2) == datetime(2028, 2, 15, tzinfo=UTC)


# --- documento (criterios 1, 4 y 9) --------------------------------------------------------------------------------

async def test_el_documento_se_genera_y_se_guarda_con_la_clave_sin_datos_personales(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    almacen = AlmacenMemoria()

    intentados = await _generador(fabrica, almacen).procesar_pendientes()

    poliza = _poliza(fabrica, susc.id)
    clave = clave_documento(susc.id, poliza.numero_poliza)
    assert intentados == 1
    assert (poliza.documento_estado, poliza.documento_s3_key, poliza.documento_error) == ("GENERADO", clave, None)
    assert poliza.documento_generado_en is not None
    contenido, tipo = almacen.objetos[clave]
    assert contenido.startswith(b"%PDF") and tipo == "application/pdf"
    assert "Ana" not in clave and "Pérez" not in clave


async def test_el_documento_de_vida_trae_todas_las_secciones_obligatorias(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    almacen = AlmacenMemoria()
    await _generador(fabrica, almacen).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)

    texto = _texto(almacen.objetos[poliza.documento_s3_key][0])

    for esperado in (
        poliza.numero_poliza,
        "23 de octubre de 2026",  # fecha de generación (reloj de la prueba)
        "Desde el 25 de octubre de 2026 hasta el 25 de octubre de 2027",
        "Ana María Pérez Gómez", "CC", "******300",  # cliente, con el documento enmascarado
        "Microseguro de vida",  # tipo de póliza
        "Valor asegurado del bien", "$ 40.000.000",
        "Naturaleza del riesgo", "fallecimiento", "Amparos cubiertos: Muerte, Incapacidad total",
        "Muerte $ 40.000.000 No aplica", "Incapacidad total $ 20.000.000 No aplica",
        "Prima anual $ 175.000", "Prima mensual $ 14.583", "Periodicidad de pago Anual", "Referencia del cobro sim_ref",
        "Condiciones generales",
    ):
        assert esperado in texto, esperado
    assert "prueba" not in texto.lower()  # la plantilla es la definitiva


async def test_el_documento_de_dispositivos_muestra_el_deducible_de_cada_amparo(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica, producto="proteccion_dispositivos", periodicidad_pago="MENSUAL")
    almacen = AlmacenMemoria()
    await _generador(fabrica, almacen).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)

    texto = _texto(almacen.objetos[poliza.documento_s3_key][0])

    for esperado in (
        "Protección de dispositivos", "robo del dispositivo",
        "Amparos cubiertos: Daño accidental, Robo",
        "Daño accidental $ 3.000.000 $ 300.000", "Robo $ 3.000.000 $ 600.000",
        "Periodicidad de pago Mensual",
    ):
        assert esperado in texto, esperado
    assert "No aplica" not in texto


async def test_los_importes_del_documento_son_los_de_la_cotizacion_aceptada(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    with fabrica() as s:
        saga = repo.obtener(s, susc.id)
    almacen = AlmacenMemoria()
    await _generador(fabrica, almacen).procesar_pendientes()

    texto = _texto(almacen.objetos[_poliza(fabrica, susc.id).documento_s3_key][0])

    assert saga.coberturas == [
        {"tipo": "muerte", "monto": 40_000_000, "deducible": 0},
        {"tipo": "incapacidad_total", "monto": 20_000_000, "deducible": 0},
    ]
    assert saga.cotizacion_version_reglas == "2026.10.1"
    assert f"$ {int(saga.prima_anual):,}".replace(",", ".") in texto  # sin recalcular: lo que guardó la saga


async def test_una_saga_sin_copia_de_coberturas_relee_la_cotizacion_sin_recalcular(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    with fabrica() as s, s.begin():
        s.get(SuscripcionDB, susc.id).coberturas = None
    cotizaciones = CotizacionesFalsas(cotizacion())
    almacen = AlmacenMemoria()

    await _generador(fabrica, almacen, cotizaciones=cotizaciones).procesar_pendientes()

    assert cotizaciones.llamadas == 1
    assert "Incapacidad total $ 20.000.000" in _texto(almacen.objetos[_poliza(fabrica, susc.id).documento_s3_key][0])


async def test_un_producto_sin_plantilla_usa_la_clave_y_un_texto_generico_sin_fallar(armar, fabrica, caplog):
    _, susc = await _cobrada(armar, fabrica)
    with fabrica() as s, s.begin():
        s.get(SuscripcionDB, susc.id).producto = "producto_nuevo"
    almacen = AlmacenMemoria()

    with caplog.at_level(logging.WARNING, logger="suscripcion.documento"):
        await _generador(fabrica, almacen).procesar_pendientes()

    poliza = _poliza(fabrica, susc.id)
    assert poliza.documento_estado == "GENERADO"
    assert "producto_nuevo" in _texto(almacen.objetos[poliza.documento_s3_key][0])
    assert "producto sin plantilla producto=producto_nuevo" in caplog.text


# --- falla de generación (criterio 8) ------------------------------------------------------------------------------

async def test_un_fallo_se_reintenta_con_espera_creciente_y_conserva_el_numero(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    numero = _poliza(fabrica, susc.id).numero_poliza
    reloj, almacen = Reloj(), AlmacenMemoria(fallos=1)
    generador = _generador(fabrica, almacen, reloj, espera_base_s=10)

    await generador.procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)
    assert (poliza.documento_estado, poliza.documento_intentos, poliza.documento_error) == ("PENDIENTE", 1, "OSError")
    assert repo.utc(poliza.documento_proximo_intento) == AHORA + timedelta(seconds=10)

    assert await generador.procesar_pendientes() == 0  # todavía no toca
    reloj.avanzar(10)
    assert await generador.procesar_pendientes() == 1

    poliza = _poliza(fabrica, susc.id)
    assert (poliza.documento_estado, poliza.documento_intentos, poliza.documento_error) == ("GENERADO", 2, None)
    assert poliza.numero_poliza == numero
    assert list(almacen.objetos) == [clave_documento(susc.id, numero)]  # la misma clave: crea una versión nueva en S3


async def test_agotados_los_reintentos_queda_fallida_visible_y_el_pago_intacto(armar, fabrica, caplog):
    _, susc = await _cobrada(armar, fabrica)
    reloj, almacen = Reloj(), AlmacenMemoria(fallos=99)
    generador = _generador(fabrica, almacen, reloj, max_intentos=3, espera_base_s=1, espera_maxima_s=5)

    with caplog.at_level(logging.ERROR, logger="suscripcion.polizas"):
        for _ in range(5):
            await generador.procesar_pendientes()
            reloj.avanzar(60)

    poliza = _poliza(fabrica, susc.id)
    assert (poliza.documento_estado, poliza.documento_intentos, poliza.documento_error) == ("FALLIDO", 3, "OSError")
    assert almacen.llamadas == 3
    with fabrica() as s:
        saga = repo.obtener(s, susc.id)
    assert (saga.estado_saga, saga.paso_actual, saga.cobro_estado) == ("COBRO_REALIZADO", "FIRMA", "APROBADO")
    assert "documento de poliza fallido" in caplog.text
    assert "Ana" not in caplog.text and "******300" not in caplog.text  # sin datos personales en los logs
    with pytest.raises(PolizaNoLista), fabrica() as s, s.begin():
        emitir_poliza(s, poliza.id, reloj())


async def test_si_el_proceso_cae_en_el_ultimo_intento_la_poliza_se_marca_fallida(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    reloj = Reloj()
    with fabrica() as s, s.begin():
        p = repo.poliza_de_suscripcion(s, susc.id)
        p.documento_intentos = 5
        p.documento_proximo_intento = AHORA - timedelta(seconds=1)

    assert await _generador(fabrica, AlmacenMemoria(), reloj, max_intentos=5).procesar_pendientes() == 0

    assert _poliza(fabrica, susc.id).documento_estado == "FALLIDO"


async def test_un_cliente_que_identidad_no_encuentra_se_reintenta(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)

    await _generador(fabrica, AlmacenMemoria(), identidad=IdentidadFalsa(sin_resumen=True)).procesar_pendientes()

    poliza = _poliza(fabrica, susc.id)
    assert (poliza.documento_estado, poliza.documento_error) == ("PENDIENTE", "DatosIncompletos")


async def test_los_logs_de_la_generacion_no_traen_datos_personales(armar, fabrica, caplog):
    await _cobrada(armar, fabrica)

    with caplog.at_level(logging.INFO):
        await _generador(fabrica, AlmacenMemoria()).procesar_pendientes()

    assert "documento de poliza generado" in caplog.text
    assert "Ana" not in caplog.text and "Pérez" not in caplog.text and "******300" not in caplog.text


# --- emisión, lista para HU21 --------------------------------------------------------------------------------------

async def test_emitir_deja_la_poliza_vigente_la_saga_emitida_y_publica_el_evento(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    await _generador(fabrica, AlmacenMemoria()).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)
    emision = AHORA + timedelta(days=1)

    with fabrica() as s, s.begin():
        emitir_poliza(s, poliza.id, emision)

    poliza = _poliza(fabrica, susc.id)
    with fabrica() as s:
        saga = repo.obtener(s, susc.id)
    assert (poliza.estado, repo.utc(poliza.fecha_emision)) == ("VIGENTE", emision)
    assert (saga.estado_saga, saga.paso_actual) == ("EMITIDA", "EMISION")
    salida = [x for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]
    assert len(salida) == 1
    assert salida[0].clave == poliza.id
    evento = Evento.model_validate(salida[0].payload)
    assert evento.event_type == "poliza_emitida"
    assert evento.correlation_id == (saga.correlacion_id or saga.id)
    assert evento.payload["numero_poliza"] == poliza.numero_poliza
    assert evento.payload["poliza_id"] == poliza.id


async def test_emitir_exige_el_documento_generado(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    with pytest.raises(PolizaNoLista), fabrica() as s, s.begin():
        emitir_poliza(s, poliza.id, AHORA)

    assert _poliza(fabrica, susc.id).estado == "PENDIENTE_FIRMA"


async def test_emitir_dos_veces_no_repite_el_evento(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    await _generador(fabrica, AlmacenMemoria()).procesar_pendientes()
    poliza = _poliza(fabrica, susc.id)

    for _ in range(2):
        with fabrica() as s, s.begin():
            emitir_poliza(s, poliza.id, AHORA)

    assert len([x for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]) == 1
