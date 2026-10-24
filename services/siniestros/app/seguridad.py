"""Verificación de claves de servicio (cabecera `X-Service-Key`)."""

from __future__ import annotations

import hmac
from collections.abc import Iterable


def clave_valida(presentada: str, validas: Iterable[str]) -> bool:
    """Compara contra todas las claves para no revelar por tiempo cuál coincidió.

    Sin claves configuradas nunca coincide: falla cerrado.
    """
    coincide = [hmac.compare_digest(presentada.encode(), k.encode()) for k in validas]
    return any(coincide)
