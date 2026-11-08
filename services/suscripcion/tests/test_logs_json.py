"""Los campos estructurados salen como claves JSON de primer nivel, redactados y sin pisar las claves base."""

import json
import logging

from app.logs import FormateadorJson


def _linea(**campos):
    registro = logging.LogRecord("solventa.prueba", logging.INFO, __file__, 1, "evento_x", None, None)
    registro.campos = campos
    return json.loads(FormateadorJson().format(registro))


def test_los_campos_son_claves_de_primer_nivel():
    linea = _linea(evento="evento_x", topic="t", offset=3)
    assert (linea["evento"], linea["topic"], linea["offset"]) == ("evento_x", "t", 3)
    assert linea["message"] == "evento_x"


def test_los_campos_no_pisan_las_claves_base():
    assert _linea(level="FAKE")["level"] == "INFO"


def test_redacta_numeros_largos_en_los_campos():
    assert "4111111111111111" not in json.dumps(_linea(ruta="/p/4111111111111111"))
