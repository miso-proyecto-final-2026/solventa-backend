"""Criterio 7 de HU02: ninguna imagen ni dato del documento aparece en los logs."""

import logging

import pytest

from app.kyc.simulador import ModoSimulador
from tests.helpers import PAYLOAD_CLIENTE, registrar
from tests.test_kyc_api import JPEG, entorno, enviar  # noqa: F401  (fixture reutilizada)

MARCA = b"CONTENIDO-SENSIBLE-DE-LA-IMAGEN"


@pytest.mark.parametrize(
    "modo", [ModoSimulador.EXITO, ModoSimulador.RECHAZO, ModoSimulador.ERROR_5XX, ModoSimulador.TIMEOUT]
)
def test_ni_las_imagenes_ni_el_documento_aparecen_en_los_logs(client, entorno, caplog, modo):  # noqa: F811
    entorno(modo)
    cliente_id, auth = registrar(client)
    imagen = JPEG + MARCA
    archivos = {
        "documento_anverso": ("a.jpg", imagen, "image/jpeg"),
        "documento_reverso": ("r.jpg", imagen, "image/jpeg"),
        "selfie": ("s.jpg", imagen, "image/jpeg"),
    }

    with caplog.at_level(logging.DEBUG):
        respuesta = enviar(client, cliente_id, auth, archivos=archivos)

    assert respuesta.status_code == 201
    registro = caplog.text
    assert MARCA.decode() not in registro
    assert repr(MARCA) not in registro
    assert PAYLOAD_CLIENTE["numero_documento"] not in registro
    assert PAYLOAD_CLIENTE["email"] not in registro


def test_los_logs_si_registran_el_estado_para_poder_operar_el_servicio(client, entorno, caplog):  # noqa: F811
    entorno(ModoSimulador.EXITO)
    cliente_id, auth = registrar(client)

    with caplog.at_level(logging.INFO, logger="identidad.kyc"):
        enviar(client, cliente_id, auth)

    assert "estado=VERIFICADO" in caplog.text
