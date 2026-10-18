"""Configuración de MS Cotización desde variables de entorno (mismo patrón que proveedores)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SQLITE_LOCAL = "sqlite:///./solventa.db"


@dataclass(frozen=True)
class ConfigCotizacion:
    reglas_version: str = "v1"
    reglas_dir: Path | None = None
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Claves de servicio con permiso de auditoría (HU14). Vacío = nadie audita.
    auditor_api_keys: tuple[str, ...] = ()

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigCotizacion:
        e = os.environ if env is None else env
        reglas_dir = e.get("REGLAS_DIR")
        return cls(
            reglas_version=e.get("REGLAS_VERSION", cls.reglas_version),
            reglas_dir=Path(reglas_dir) if reglas_dir else None,
            database_url=e.get("COTIZACION_DATABASE_URL")
            or e.get("DATABASE_URL")
            or cls.database_url,
            auditor_api_keys=tuple(
                k.strip() for k in e.get("AUDITOR_API_KEYS", "").split(",") if k.strip()
            ),
        )
