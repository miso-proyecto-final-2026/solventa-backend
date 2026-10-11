import time
from threading import Lock
from typing import Any

_TTL_SEGUNDOS = 5 * 60
_cache: dict[str, tuple[float, int, Any]] = {}
_lock = Lock()


def obtener(clave: str) -> tuple[int, Any] | None:
    with _lock:
        entrada = _cache.get(clave)
        if entrada is None:
            return None
        expira_en, status_code, cuerpo = entrada
        if time.monotonic() > expira_en:
            del _cache[clave]
            return None
        return status_code, cuerpo


def guardar(clave: str, status_code: int, cuerpo: Any) -> None:
    with _lock:
        _cache[clave] = (time.monotonic() + _TTL_SEGUNDOS, status_code, cuerpo)
