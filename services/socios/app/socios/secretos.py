"""Hash de los `client_secret` de los socios (HU32, criterio 11). Bcrypt: el secreto nunca se guarda en claro.

bcrypt es lento a propósito (~100-250 ms con el costo por defecto): solo se usa en `/oauth/token`, nunca en la ruta
de la cotización, que valida el JWT sin tocar la base.
"""

from __future__ import annotations

from functools import cache

import bcrypt

RONDAS_POR_DEFECTO = 12
# bcrypt solo usa los primeros 72 bytes; un secreto más largo se rechaza en lugar de truncarse en silencio.
LONGITUD_MAXIMA_BYTES = 72


def hash_secreto(secreto: str, rondas: int = RONDAS_POR_DEFECTO) -> str:
    datos = secreto.encode("utf-8")
    if not datos or len(datos) > LONGITUD_MAXIMA_BYTES:
        raise ValueError(f"El secreto debe tener entre 1 y {LONGITUD_MAXIMA_BYTES} bytes.")
    return bcrypt.hashpw(datos, bcrypt.gensalt(rounds=rondas)).decode("ascii")


def verificar_secreto(secreto: str, hash_guardado: str) -> bool:
    datos = secreto.encode("utf-8")
    if not datos or len(datos) > LONGITUD_MAXIMA_BYTES:
        return False
    try:
        return bcrypt.checkpw(datos, hash_guardado.encode("ascii"))
    except ValueError:
        return False


@cache
def hash_ficticio() -> str:
    """Hash con el mismo costo que uno real: se compara contra él cuando el `client_id` no existe.

    Así la respuesta tarda lo mismo exista o no el socio y el tiempo no revela qué `client_id` son válidos.
    """
    return hash_secreto("socio-inexistente")
