"""Configuración de MS Perfilamiento desde variables de entorno."""

from __future__ import annotations

import os
from collections.abc import Mapping
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


PREFIJO_CLAVE_CON_NOMBRE = "PERFILAMIENTO_API_KEY_"


def _claves_con_nombre(env: Mapping[str, str]) -> dict[str, str]:
    """`PERFILAMIENTO_API_KEY_COTIZACION=k` → {k: "cotizacion"} (HU24).

    Una variable por llamador permite que cada uno tenga su propio secreto (en staging cada secreto
    es una sola cadena) y que la auditoría sepa quién consultó. Una variable vacía se ignora.
    """
    return {
        valor.strip(): nombre[len(PREFIJO_CLAVE_CON_NOMBRE) :].lower()
        for nombre, valor in env.items()
        if nombre.startswith(PREFIJO_CLAVE_CON_NOMBRE)
        and len(nombre) > len(PREFIJO_CLAVE_CON_NOMBRE)
        and valor.strip()
    }


@dataclass(frozen=True)
class ConfigPerfilamiento:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Clave de servicio → nombre del llamador (el «origen» que queda en el intento bloqueado y en
    # la consulta del linaje). Sale de PERFILAMIENTO_API_KEYS y de PERFILAMIENTO_API_KEY_<NOMBRE>.
    # Vacío = nadie accede (falla cerrado).
    api_keys: dict[str, str] | None = None

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigPerfilamiento:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("PERFILAMIENTO_DATABASE_URL") or e.get("DATABASE_URL"),
            api_keys=_claves(e.get("PERFILAMIENTO_API_KEYS", "")) | _claves_con_nombre(e),
        )
