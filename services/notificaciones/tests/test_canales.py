"""Adaptadores de envío (ADR-0004): el push simulado, el correo simulado y Amazon SES con un doble de boto3."""

import logging

import pytest
from app.notificaciones.canales import CorreoSes, CorreoSimulado, PushSimulado
from app.notificaciones.dominio import (
    Correo,
    ErrorEnvio,
    Prioridad,
    Push,
    TipoNotificacion,
)
from app.notificaciones.modelos_db import BandejaPushDB, EnvioDB
from sqlalchemy import select

from tests.dobles import AHORA, CORREO_CLIENTE

CORREO = Correo(destinatario=CORREO_CLIENTE, asunto="Tu póliza está activa", texto="Hola", html="<p>Hola</p>")


def _push(envio_id="envio-1") -> Push:
    return Push(
        envio_id=envio_id, cliente_id="cli-1", titulo="Suscripción completada", texto="Tu póliza ya está activa.",
        titulo_publico="Solventa", texto_publico="Tienes una notificación nueva.", prioridad=Prioridad.ALTA,
        tipo=TipoNotificacion.POLIZA_EMITIDA, suscripcion_id="susc-1", idioma="es-CO",
    )


def _crear_envio(fabrica, envio_id="envio-1"):
    with fabrica() as s, s.begin():
        s.add(
            EnvioDB(
                id=envio_id, evento_origen_id=f"ev-{envio_id}", tipo="POLIZA_EMITIDA", canal="PUSH", cliente_id="cli-1",
                idioma="es-CO", datos={}, estado="PENDIENTE", intentos=0, creado_en=AHORA,
            )
        )


# --- push simulado ---------------------------------------------------------------------------------------------------

async def test_el_push_simulado_deja_el_mensaje_en_la_bandeja(fabrica, reloj):
    _crear_envio(fabrica)
    canal = PushSimulado(fabrica, reloj=reloj)

    referencia = await canal.entregar(_push())

    assert referencia == "bandeja:envio-1"
    with fabrica() as s:
        (fila,) = s.execute(select(BandejaPushDB)).scalars()
    assert (fila.cliente_id, fila.prioridad, fila.titulo, fila.texto_publico) == (
        "cli-1", "ALTA", "Suscripción completada", "Tienes una notificación nueva.",
    )
    assert fila.creado_en.replace(tzinfo=AHORA.tzinfo) == AHORA and fila.recibida_en is None


async def test_entregar_dos_veces_el_mismo_envio_deja_una_sola_fila(fabrica, reloj):
    _crear_envio(fabrica)
    canal = PushSimulado(fabrica, reloj=reloj)

    await canal.entregar(_push())
    await canal.entregar(_push())  # un reintento tras caer a mitad

    with fabrica() as s:
        assert len(list(s.execute(select(BandejaPushDB)).scalars())) == 1


async def test_el_push_simulado_no_escribe_el_texto_en_el_log(fabrica, reloj, caplog):
    _crear_envio(fabrica)
    with caplog.at_level(logging.DEBUG):
        await PushSimulado(fabrica, reloj=reloj).entregar(_push())

    assert "envio_id=envio-1" in caplog.text and "Tu póliza ya está activa" not in caplog.text


# --- correo simulado -------------------------------------------------------------------------------------------------

async def test_el_correo_simulado_cuenta_los_envios_y_no_registra_la_direccion(caplog):
    canal = CorreoSimulado()

    with caplog.at_level(logging.DEBUG):
        primera = await canal.enviar(CORREO)
        segunda = await canal.enviar(CORREO)

    assert canal.enviados == 2 and primera != segunda and primera.startswith("simulado-")
    assert CORREO_CLIENTE not in caplog.text


# --- Amazon SES ------------------------------------------------------------------------------------------------------

class SesFalso:
    def __init__(self, error=None):
        self.llamadas: list[dict] = []
        self.error = error

    def send_email(self, **kwargs):
        self.llamadas.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"MessageId": "0100018f-ejemplo"}


class ErrorDeAws(Exception):
    def __init__(self, codigo, mensaje):
        super().__init__(mensaje)
        self.response = {"Error": {"Code": codigo, "Message": mensaje}}


async def test_ses_envia_con_el_remitente_el_destinatario_y_las_dos_versiones_del_cuerpo():
    ses = SesFalso()

    referencia = await CorreoSes("notificaciones@solventa.example", cliente=ses).enviar(CORREO)

    assert referencia == "0100018f-ejemplo"
    (llamada,) = ses.llamadas
    assert llamada["Source"] == "notificaciones@solventa.example"
    assert llamada["Destination"] == {"ToAddresses": [CORREO_CLIENTE]}
    assert llamada["Message"]["Subject"] == {"Data": "Tu póliza está activa", "Charset": "UTF-8"}
    assert llamada["Message"]["Body"]["Text"]["Data"] == "Hola" and llamada["Message"]["Body"]["Html"]["Data"] == "<p>Hola</p>"


async def test_un_fallo_de_ses_se_reduce_al_codigo_del_error_sin_su_mensaje():
    ses = SesFalso(ErrorDeAws("MessageRejected", f"Email address is not verified: {CORREO_CLIENTE}"))

    with pytest.raises(ErrorEnvio) as error:
        await CorreoSes("notificaciones@solventa.example", cliente=ses).enviar(CORREO)

    assert str(error.value) == "MessageRejected" and CORREO_CLIENTE not in str(error.value)
    assert error.value.__cause__ is None  # tampoco queda encadenada la excepción original con la dirección


async def test_un_fallo_sin_respuesta_de_aws_se_reduce_al_nombre_de_la_clase():
    ses = SesFalso(TimeoutError(f"timeout enviando a {CORREO_CLIENTE}"))

    with pytest.raises(ErrorEnvio) as error:
        await CorreoSes("notificaciones@solventa.example", cliente=ses).enviar(CORREO)

    assert str(error.value) == "TimeoutError" and CORREO_CLIENTE not in str(error.value)


def test_ses_exige_un_remitente():
    with pytest.raises(ValueError, match="CORREO_REMITENTE"):
        CorreoSes("", cliente=SesFalso())
