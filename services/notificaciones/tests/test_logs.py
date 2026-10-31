"""Los logs no llevan correos ni números de tarjeta, vengan de donde vengan."""

import io
import json
import logging

from app.logs import (
    ENMASCARADO_CORREO,
    ENMASCARADO_PAN,
    FiltroDatosPersonales,
    FormateadorJson,
    redactar,
)


def test_redacta_correos_y_numeros_de_tarjeta():
    assert redactar("enviado a ana.perez+x@example.com ok") == f"enviado a {ENMASCARADO_CORREO} ok"
    assert redactar("tarjeta 4111 1111 1111 1111 fin") == f"tarjeta {ENMASCARADO_PAN} fin"


def test_no_confunde_un_uuid_con_una_tarjeta():
    identificador = "3f85ff64-f746-40b3-9725-48489248206a"
    assert redactar(f"poliza_id={identificador}") == f"poliza_id={identificador}"


def _logger():
    salida = io.StringIO()
    manejador = logging.StreamHandler(salida)
    manejador.setFormatter(FormateadorJson())
    manejador.addFilter(FiltroDatosPersonales())
    log = logging.getLogger("prueba.logs.notificaciones")
    log.handlers, log.propagate = [manejador], False
    log.setLevel(logging.DEBUG)
    return log, salida


def test_redacta_el_mensaje_sus_argumentos_y_la_traza_de_una_excepcion():
    log, salida = _logger()

    log.info("a %s", "ana@example.com")
    try:
        raise RuntimeError("falló con ana@example.com")
    except RuntimeError:
        log.exception("error")

    lineas = [json.loads(x) for x in salida.getvalue().splitlines()]
    assert "ana@example.com" not in salida.getvalue()
    assert lineas[0]["message"] == f"a {ENMASCARADO_CORREO}" and ENMASCARADO_CORREO in lineas[1]["exception"]
