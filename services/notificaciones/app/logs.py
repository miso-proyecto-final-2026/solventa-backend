"""Logs estructurados en JSON con `trace_id` y `correlation_id`, sin correos ni números de tarjeta.

El código de Notificaciones nunca pasa el correo del cliente a un log. `FiltroDatosPersonales` es la segunda defensa:
enmascara cualquier dirección de correo y cualquier secuencia con forma de PAN en el mensaje, en sus argumentos y en la
traza de una excepción, venga de este servicio o de una librería (boto3, httpx).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from app.contexto import correlacion_id, traza_id

ENMASCARADO_CORREO = "[correo-redactado]"
ENMASCARADO_PAN = "[pan-redactado]"
_CORREO = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PAN = re.compile(r"(?<![0-9A-Za-z])(?:\d[ -]?){12,18}\d(?![0-9A-Za-z])")


def redactar(texto: str) -> str:
    return _PAN.sub(ENMASCARADO_PAN, _CORREO.sub(ENMASCARADO_CORREO, texto))


class FiltroDatosPersonales(logging.Filter):
    def filter(self, registro: logging.LogRecord) -> bool:
        registro.msg = redactar(registro.getMessage())
        registro.args = None
        if registro.exc_info and registro.exc_info[1] is not None:
            registro.exc_text = redactar(logging.Formatter().formatException(registro.exc_info))
            registro.exc_info = None
        return True


class FormateadorJson(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        linea: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(registro.created, UTC).isoformat(),
            "level": registro.levelname,
            "logger": registro.name,
            "message": registro.getMessage(),
            "trace_id": traza_id.get(),
            "correlation_id": correlacion_id.get(),
        }
        if registro.exc_text:
            linea["exception"] = registro.exc_text
        elif registro.exc_info:
            linea["exception"] = redactar(self.formatException(registro.exc_info))
        return json.dumps(linea, ensure_ascii=False)


def configurar_logs(nivel: int = logging.INFO) -> None:
    """Un único manejador en la raíz: todo lo que se registre (servicio, uvicorn, aiokafka) sale en JSON y redactado."""
    manejador = logging.StreamHandler()
    manejador.setFormatter(FormateadorJson())
    manejador.addFilter(FiltroDatosPersonales())
    raiz = logging.getLogger()
    raiz.handlers = [manejador]
    raiz.setLevel(nivel)
    for nombre in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        registro = logging.getLogger(nombre)
        registro.handlers = []
        registro.propagate = True
