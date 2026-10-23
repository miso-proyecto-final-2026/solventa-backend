"""Configuración de MS Siniestros desde variables de entorno.

La conexión a Kafka (`KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD`) la lee `app/eventos` (plantilla HS2).
"""

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


@dataclass(frozen=True)
class ConfigSiniestros:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Clave de proveedor → fuente que representa. Vacío = nadie accede (falla cerrado).
    api_keys: dict[str, str] | None = None
    # None = sin configurar; solo en modo desarrollo se sustituye por un publicador en memoria.
    kafka_brokers: str | None = None

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigSiniestros:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("SINIESTROS_DATABASE_URL") or e.get("DATABASE_URL"),
            api_keys=_claves(e.get("SINIESTROS_API_KEYS", "")),
            kafka_brokers=e.get("KAFKA_BOOTSTRAP_SERVERS") or None,
        )
