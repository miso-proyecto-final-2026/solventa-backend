"""Configuración externa (variables de entorno / ConfigMap) de la integración."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigProveedores:
    timeout_s: float = 0.7
    umbral_fallos: int = 5
    intervalo_half_open_s: float = 30.0
    ttl_s: int = 300
    jitter_min: float = 0.10
    jitter_max: float = 0.15
    ttl_respaldo_s: int = 86400
    redis_url: str | None = None

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigProveedores:
        e = os.environ if env is None else env
        return cls(
            timeout_s=float(e.get("PROVEEDORES_TIMEOUT_S", cls.timeout_s)),
            umbral_fallos=int(e.get("PROVEEDORES_UMBRAL_FALLOS", cls.umbral_fallos)),
            intervalo_half_open_s=float(
                e.get("PROVEEDORES_INTERVALO_HALF_OPEN_S", cls.intervalo_half_open_s)
            ),
            ttl_s=int(e.get("PROVEEDORES_TTL_S", cls.ttl_s)),
            jitter_min=float(e.get("PROVEEDORES_JITTER_MIN", cls.jitter_min)),
            jitter_max=float(e.get("PROVEEDORES_JITTER_MAX", cls.jitter_max)),
            ttl_respaldo_s=int(e.get("PROVEEDORES_TTL_RESPALDO_S", cls.ttl_respaldo_s)),
            redis_url=e.get("REDIS_URL"),
        )
