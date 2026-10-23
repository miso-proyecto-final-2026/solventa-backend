"""Configuración de MS Siniestros desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./siniestros.db"


def _claves(crudo: str) -> dict[str, str]:
    """`fuente=clave,otra=clave2` → {clave: fuente}. El nombre de la clave es la fuente del proveedor."""
    claves: dict[str, str] = {}
    for parte in crudo.split(","):
        nombre, separador, clave = parte.strip().partition("=")
        if separador and nombre.strip() and clave.strip():
            claves[clave.strip()] = nombre.strip()
    return claves


def _entero(crudo: str | None, por_defecto: int) -> int:
    try:
        return int(crudo) if crudo else por_defecto
    except ValueError:
        return por_defecto


@dataclass(frozen=True)
class ConfigSiniestros:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Clave de proveedor → fuente que representa. Vacío = nadie accede (falla cerrado).
    api_keys: dict[str, str] | None = None
    particiones: int = 6
    # Mensajes sin consumir que el bus acepta antes de aplicar contrapresión (503 al proveedor).
    capacidad_rezago: int = 200_000
    # Intentos del consumidor antes de enviar el evento a la cola de mensajes fallidos.
    reintentos_consumidor: int = 3
    espera_base_consumidor_s: float = 0.2

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigSiniestros:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("SINIESTROS_DATABASE_URL") or e.get("DATABASE_URL"),
            api_keys=_claves(e.get("SINIESTROS_API_KEYS", "")),
            particiones=_entero(e.get("SINIESTROS_PARTICIONES"), 6),
            capacidad_rezago=_entero(e.get("SINIESTROS_CAPACIDAD_REZAGO"), 200_000),
            reintentos_consumidor=_entero(e.get("SINIESTROS_REINTENTOS_CONSUMIDOR"), 3),
        )
