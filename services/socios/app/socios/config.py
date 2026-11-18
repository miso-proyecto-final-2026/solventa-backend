"""Configuración de MS Socios desde el entorno. Falla cerrado fuera de desarrollo (ver app/configuracion.py)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.configuracion import ErrorConfiguracion, permitir_sustituto_local

# Solo para desarrollo local: nunca se usa si falta SOCIOS_JWT_SECRETO fuera de SOLVENTA_MODO_DESARROLLO.
SECRETO_JWT_DESARROLLO = "solo-desarrollo-socios-no-usar-en-staging-0123456789"
LONGITUD_MINIMA_SECRETO = 32


@dataclass(frozen=True)
class ConfigSocios:
    database_url: str
    redis_url: str | None
    cotizacion_url: str
    jwt_secreto: str
    jwt_ttl_s: int = 3600
    timeout_cotizacion_s: float = 3.0
    ttl_idempotencia_s: int = 24 * 3600
    ttl_contratos_s: float = 60.0

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigSocios:
        e = os.environ if env is None else env

        database_url = e.get("SOCIOS_DATABASE_URL") or e.get("DATABASE_URL")
        if not database_url:
            permitir_sustituto_local("DATABASE_URL", "SQLite local", e)
            database_url = "sqlite:///./socios-local.db"

        redis_url = e.get("REDIS_URL") or None
        if redis_url is None:
            permitir_sustituto_local("REDIS_URL", "un almacén en memoria (cuota e idempotencia por réplica)", e)

        cotizacion_url = e.get("COTIZACION_URL")
        if not cotizacion_url:
            permitir_sustituto_local("COTIZACION_URL", "http://localhost:8000", e)
            cotizacion_url = "http://localhost:8000"

        secreto = e.get("SOCIOS_JWT_SECRETO")
        if not secreto:
            permitir_sustituto_local("SOCIOS_JWT_SECRETO", "un secreto de desarrollo", e)
            secreto = SECRETO_JWT_DESARROLLO
        if len(secreto) < LONGITUD_MINIMA_SECRETO:
            raise ErrorConfiguracion(f"SOCIOS_JWT_SECRETO debe tener al menos {LONGITUD_MINIMA_SECRETO} caracteres.")

        return cls(
            database_url=database_url,
            redis_url=redis_url,
            cotizacion_url=cotizacion_url,
            jwt_secreto=secreto,
            jwt_ttl_s=int(e.get("SOCIOS_JWT_TTL_S", "3600")),
            timeout_cotizacion_s=float(e.get("SOCIOS_TIMEOUT_COTIZACION_S", "3.0")),
        )
