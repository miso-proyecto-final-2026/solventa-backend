"""Configuración del registro de MS Cotización.

Sin esto el nivel de la raíz es WARNING y ningún `log.info` de la aplicación llega a la salida de los pods (incluido el
de latencia que ya existía). Va a la salida estándar, que es de donde Kubernetes recoge los logs.
"""

import logging
import os
import sys

_FORMATO = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configurar_logs() -> None:
    """INFO por defecto; `LOG_LEVEL` lo cambia. Se puede llamar más de una vez sin duplicar manejadores."""
    nivel = logging.getLevelNamesMapping().get(
        os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    if not any(getattr(h, "_solventa", False) for h in raiz.handlers):
        manejador = logging.StreamHandler(sys.stdout)
        manejador.setFormatter(logging.Formatter(_FORMATO))
        manejador._solventa = True  # marca para no agregarlo dos veces
        raiz.addHandler(manejador)
