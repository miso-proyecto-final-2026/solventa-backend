"""Certificado de cobertura (HU22): contenido por idioma, descarga del titular, idempotencia y enlaces firmados."""

import base64
import json
import logging
from datetime import timedelta

import pytest
from app.suscripcion.certificado import (
    FirmadorEnlaces,
    ServicioCertificado,
    clave_certificado,
    normalizar_idioma,
)
from app.suscripcion.errores import (
    CertificadoNoDisponible,
    EnlaceInvalido,
    EnlacesNoConfigurados,
    EnlaceVencido,
    NoAutenticado,
    PolizaNoEncontrada,
    SuscripcionNoEncontrada,
)

from tests.dobles import IdentidadFalsa
from tests.test_firma import _firmar, _lista, _poliza, _texto

CLAVE = "clave-de-enlaces-para-pruebas-32"


class AlmacenContado:
    """El almacén en memoria de las pruebas, que además cuenta cuántas veces se escribió cada clave."""

    def __init__(self, base):
        self.base = base
        self.escrituras: list[str] = []

    def guardar(self, clave, contenido, content_type):
        self.escrituras.append(clave)
        self.base.guardar(clave, contenido, content_type)

    def leer(self, clave):
        return self.base.leer(clave)


async def _emitida(armar, fabrica, *, con_firmador=True, identidad=None):
    """Una póliza firmada y emitida, con su servicio de certificados sobre el mismo almacén y el mismo reloj."""
    firma, susc, almacen, reloj = await _lista(armar, fabrica)
    await _firmar(firma, susc)
    contado = AlmacenContado(almacen)
    servicio = ServicioCertificado(
        fabrica=fabrica,
        identidad=identidad or IdentidadFalsa(),
        almacen=contado,
        firmador=FirmadorEnlaces(CLAVE, timedelta(hours=24)) if con_firmador else None,
        reloj=reloj,
    )
    return servicio, susc, contado, reloj


# --- idioma -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cabecera", "esperado"),
    [
        (None, "es-CO"),
        ("", "es-CO"),
        ("es-CO", "es-CO"),
        ("es-419,es;q=0.9", "es-CO"),
        ("en", "en-US"),
        ("en-GB", "en-US"),
        ("en-US,en;q=0.9,es;q=0.8", "en-US"),
        ("fr-FR", "es-CO"),  # un idioma que no hay cae al por defecto
    ],
)
def test_el_idioma_sale_de_accept_language_y_cae_a_es_co(cabecera, esperado):
    assert normalizar_idioma(cabecera) == esperado


# --- contenido (criterios 1 y 9) -----------------------------------------------------------------------------------------

async def test_el_certificado_en_espanol_repite_los_datos_de_la_poliza_sin_recalcular(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    certificado = await servicio.descargar_titular(susc.id, "token-1", "es-CO")

    assert certificado.contenido.startswith(b"%PDF") and certificado.numero_poliza == poliza.numero_poliza
    texto = _texto(certificado.contenido)
    assert "Certificado de cobertura" in texto and "Cobertura activa" in texto
    assert poliza.numero_poliza in texto
    assert "Ana María Pérez Gómez" in texto and "CC ******300" in texto
    assert "Microseguro de vida" in texto and "Muerte" in texto and "Incapacidad total" in texto
    assert "$ 175.000 al año" in texto and "$ 40.000.000" in texto  # los importes de la cotización aceptada
    assert "26 de octubre de 2026" in texto or "25 de octubre de 2026" in texto
    assert "documento completo" not in texto  # no es la póliza: no repite sus condiciones generales


async def test_el_certificado_en_ingles_traduce_los_textos_y_los_formatos(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)

    texto = _texto((await servicio.descargar_titular(susc.id, "token-1", "en-US")).contenido)

    assert "Certificate of coverage" in texto and "Active coverage" in texto and "Policy number" in texto
    assert "Micro life insurance" in texto and "Death" in texto and "Total disability" in texto
    assert "COP 175,000 per year" in texto and "COP 40,000,000" in texto
    assert "October 2" in texto  # 25 o 26 según la zona horaria
    assert "Certificado" not in texto and "Cobertura activa" not in texto


async def test_un_producto_o_amparo_sin_traduccion_no_hace_fallar_el_certificado(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)
    from app.suscripcion.modelos_db import SuscripcionDB

    with fabrica() as s, s.begin():
        saga = s.get(SuscripcionDB, susc.id)
        saga.producto = "producto_nuevo"
        saga.coberturas = [{"tipo": "inundacion", "monto": 5_000_000, "deducible": 100_000}]

    texto = _texto((await servicio.descargar_titular(susc.id, "token-1", "es-CO")).contenido)

    assert "producto_nuevo" in texto and "Inundacion" in texto and "$ 100.000" in texto


# --- idempotencia y almacenamiento ------------------------------------------------------------------------------------------

async def test_se_genera_una_vez_se_guarda_en_certificados_y_la_segunda_vez_se_entrega_la_copia(armar, fabrica):
    servicio, susc, almacen, _ = await _emitida(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    primero = await servicio.descargar_titular(susc.id, "token-1", "es-CO")
    segundo = await servicio.descargar_titular(susc.id, "token-1", "es-CO")

    clave = clave_certificado(susc.id, poliza.numero_poliza, "es-CO")
    assert almacen.escrituras == [clave] and clave.startswith("certificados/") and "Ana" not in clave
    assert primero.contenido == segundo.contenido


async def test_cada_idioma_tiene_su_propia_copia(armar, fabrica):
    servicio, susc, almacen, _ = await _emitida(armar, fabrica)

    await servicio.descargar_titular(susc.id, "token-1", "es-CO")
    await servicio.descargar_titular(susc.id, "token-1", "en-US")

    assert len(set(almacen.escrituras)) == 2 and {c.rsplit("-", 1)[-1] for c in almacen.escrituras} == {"CO.pdf", "US.pdf"}


async def test_el_log_de_generacion_no_lleva_datos_personales(armar, fabrica, caplog):
    servicio, susc, *_ = await _emitida(armar, fabrica)

    with caplog.at_level(logging.DEBUG, logger="suscripcion.certificado"):
        await servicio.descargar_titular(susc.id, "token-1", "es-CO")

    assert "certificado generado" in caplog.text
    assert "Ana" not in caplog.text and "Pérez" not in caplog.text and "******300" not in caplog.text


# --- acceso (criterio 7) ---------------------------------------------------------------------------------------------------

async def test_otro_cliente_no_descarga_el_certificado_y_se_le_responde_como_inexistente(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)

    with pytest.raises(SuscripcionNoEncontrada):
        await servicio.descargar_titular(susc.id, "token-2", "es-CO")
    with pytest.raises(SuscripcionNoEncontrada):
        await servicio.descargar_titular("no-existe", "token-1", "es-CO")


async def test_sin_sesion_o_con_sesion_invalida_no_hay_certificado(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)

    with pytest.raises(NoAutenticado):
        await servicio.descargar_titular(susc.id, None, "es-CO")
    with pytest.raises(NoAutenticado):
        await servicio.descargar_titular(susc.id, "token-desconocido", "es-CO")


async def test_una_poliza_que_aun_no_esta_emitida_no_tiene_certificado(armar, fabrica):
    _firma, susc, almacen, reloj = await _lista(armar, fabrica)  # cobrada y con el PDF, pero sin firmar
    servicio = ServicioCertificado(fabrica=fabrica, identidad=IdentidadFalsa(), almacen=almacen, firmador=None, reloj=reloj)

    with pytest.raises(CertificadoNoDisponible):
        await servicio.descargar_titular(susc.id, "token-1", "es-CO")


async def test_si_identidad_no_conoce_al_cliente_no_se_inventa_un_certificado(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica, identidad=IdentidadFalsa(sin_resumen=True))

    with pytest.raises(CertificadoNoDisponible):
        await servicio.descargar_titular(susc.id, "token-1", "es-CO")


# --- enlaces firmados (criterios 3 y 7) ---------------------------------------------------------------------------------------

async def test_el_enlace_vigente_descarga_el_certificado_sin_sesion(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    token, vence = await servicio.emitir_enlace(poliza.id, "es-CO")
    certificado = await servicio.descargar_por_enlace(token)

    assert certificado.numero_poliza == poliza.numero_poliza and certificado.contenido.startswith(b"%PDF")
    assert vence - servicio._reloj() == timedelta(hours=24)


async def test_el_enlace_respeta_el_idioma_con_que_se_emitio(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    token, _ = await servicio.emitir_enlace(poliza.id, "en-US")

    assert "Certificate of coverage" in _texto((await servicio.descargar_por_enlace(token)).contenido)


async def test_un_enlace_vencido_se_rechaza_y_se_distingue_de_uno_invalido(armar, fabrica):
    servicio, susc, _, reloj = await _emitida(armar, fabrica)
    token, _ = await servicio.emitir_enlace(_poliza(fabrica, susc.id).id, "es-CO")

    reloj.ahora += timedelta(hours=23, minutes=59)
    await servicio.descargar_por_enlace(token)  # aún vale

    reloj.ahora += timedelta(minutes=2)
    with pytest.raises(EnlaceVencido):
        await servicio.descargar_por_enlace(token)


async def test_un_enlace_alterado_o_de_otra_clave_se_rechaza(armar, fabrica):
    servicio, susc, _, reloj = await _emitida(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)
    token, _ = await servicio.emitir_enlace(poliza.id, "es-CO")
    cuerpo, firma = token.split(".")

    # Cambiar la póliza del payload sin poder volver a firmarlo.
    datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
    datos["p"] = "otra-poliza"
    alterado = base64.urlsafe_b64encode(json.dumps(datos, separators=(",", ":")).encode()).rstrip(b"=").decode()
    ajeno, _ = FirmadorEnlaces("otra-clave-distinta-de-32-bytes!!", timedelta(hours=24)).emitir(poliza.id, "es-CO", reloj())

    for malo in (f"{alterado}.{firma}", ajeno, f"{cuerpo}.firma-inventada", cuerpo, "", "basura", "a.b.c", f"{cuerpo}."):
        with pytest.raises(EnlaceInvalido):
            await servicio.descargar_por_enlace(malo)


async def test_ampliar_el_vencimiento_dentro_del_token_invalida_la_firma(armar, fabrica):
    servicio, susc, _, reloj = await _emitida(armar, fabrica)
    token, _ = await servicio.emitir_enlace(_poliza(fabrica, susc.id).id, "es-CO")
    cuerpo, firma = token.split(".")
    datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
    datos["e"] += 10 * 365 * 24 * 3600
    ampliado = base64.urlsafe_b64encode(json.dumps(datos, separators=(",", ":")).encode()).rstrip(b"=").decode()
    reloj.ahora += timedelta(days=2)

    with pytest.raises(EnlaceInvalido):
        await servicio.descargar_por_enlace(f"{ampliado}.{firma}")


async def test_no_se_emite_enlace_para_una_poliza_inexistente(armar, fabrica):
    servicio, *_ = await _emitida(armar, fabrica)

    with pytest.raises(PolizaNoEncontrada):
        await servicio.emitir_enlace("no-existe", "es-CO")


async def test_no_se_emite_enlace_para_una_poliza_que_aun_no_esta_emitida(armar, fabrica):
    _, susc, almacen, reloj = await _lista(armar, fabrica)  # cobrada y con el PDF, pero sin firmar
    servicio = ServicioCertificado(
        fabrica=fabrica,
        identidad=IdentidadFalsa(),
        almacen=almacen,
        firmador=FirmadorEnlaces(CLAVE, timedelta(hours=24)),
        reloj=reloj,
    )

    with pytest.raises(PolizaNoEncontrada):
        await servicio.emitir_enlace(_poliza(fabrica, susc.id).id, "es-CO")


async def test_sin_clave_de_enlaces_no_se_emiten_ni_se_aceptan_pero_el_titular_si_descarga(armar, fabrica):
    servicio, susc, *_ = await _emitida(armar, fabrica, con_firmador=False)

    with pytest.raises(EnlacesNoConfigurados):
        await servicio.emitir_enlace(_poliza(fabrica, susc.id).id, "es-CO")
    with pytest.raises(EnlacesNoConfigurados):
        await servicio.descargar_por_enlace("cualquiera.token")
    assert (await servicio.descargar_titular(susc.id, "token-1", "es-CO")).contenido.startswith(b"%PDF")


def test_la_clave_de_los_enlaces_no_puede_ser_corta():
    with pytest.raises(ValueError):
        FirmadorEnlaces("corta", timedelta(hours=1))
