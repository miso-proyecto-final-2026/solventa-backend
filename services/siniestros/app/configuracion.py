"""Configuración obligatoria fuera de desarrollo (falla cerrado).

Cada dependencia externa tiene un sustituto local para trabajar sin infraestructura
(consentimiento siempre otorgado, caché en memoria, SQLite). Esos sustitutos solo se
aceptan con `SOLVENTA_MODO_DESARROLLO=1`; en cualquier otro caso, una variable
faltante impide arrancar en lugar de degradar el servicio en silencio.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

VARIABLE_MODO_DESARROLLO = "SOLVENTA_MODO_DESARROLLO"
_VERDADERO = {"1", "true", "si", "sí", "yes"}


class ErrorConfiguracion(RuntimeError):
    """Falta configuración obligatoria; el servicio no debe arrancar."""


def modo_desarrollo(env: dict[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return e.get(VARIABLE_MODO_DESARROLLO, "").strip().lower() in _VERDADERO


def permitir_sustituto_local(
    variable: str, sustituto: str, env: dict[str, str] | None = None
) -> None:
    """Se llama cuando falta `variable`. En desarrollo avisa; si no, lanza ErrorConfiguracion."""
    if modo_desarrollo(env):
        log.warning(
            "%s no está definido: se usa %s (solo desarrollo)", variable, sustituto
        )
        return
    raise ErrorConfiguracion(
        f"{variable} no está definido. Defínalo, o use {VARIABLE_MODO_DESARROLLO}=1 "
        f"para trabajar con {sustituto} (solo desarrollo)."
    )
