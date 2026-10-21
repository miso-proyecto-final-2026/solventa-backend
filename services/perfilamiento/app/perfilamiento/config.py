"""Configuración de MS Perfilamiento desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./perfilamiento.db"


def _claves(crudo: str) -> dict[str, str]:
    """`nombre=clave,otro=clave2` → {clave: nombre}. Una clave sin nombre se llama «servicio»."""
    claves: dict[str, str] = {}
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        nombre, separador, clave = parte.partition("=")
        if separador:
            claves[clave.strip()] = nombre.strip() or "servicio"
        else:
            claves[nombre] = "servicio"
    return claves


@dataclass(frozen=True)
class ConfigPerfilamiento:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Clave de servicio → nombre del llamador (el «origen» que queda en el intento bloqueado).
    # Vacío = nadie accede (falla cerrado).
    api_keys: dict[str, str] | None = None

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigPerfilamiento:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("PERFILAMIENTO_DATABASE_URL") or e.get("DATABASE_URL"),
            api_keys=_claves(e.get("PERFILAMIENTO_API_KEYS", "")),
        )
