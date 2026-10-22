"""Configuración externa (variables de entorno / ConfigMap) de la integración con las fuentes."""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.proveedores.dominio import Fuente


@dataclass(frozen=True)
class ConfigProveedores:
    timeout_s: float = 0.7
    umbral_fallos: int = 5
    intervalo_half_open_s: float = 30.0
    # TTL por tipo de dato (valor inicial del ejemplo de linaje, a confirmar): las señales
    # financieras cambian más que las socioeconómicas.
    ttl_open_finance_s: int = 3600
    ttl_open_data_s: int = 86400
    jitter_min: float = 0.10
    jitter_max: float = 0.15
    ttl_respaldo_s: int = 7 * 86400
    redis_url: str | None = None

    def ttl_de(self, fuente: Fuente) -> int:
        return self.ttl_open_finance_s if fuente is Fuente.OPEN_FINANCE else self.ttl_open_data_s

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigProveedores:
        e = os.environ if env is None else env
        return cls(
            timeout_s=float(e.get("PROVEEDORES_TIMEOUT_S", cls.timeout_s)),
            umbral_fallos=int(e.get("PROVEEDORES_UMBRAL_FALLOS", cls.umbral_fallos)),
            intervalo_half_open_s=float(
                e.get("PROVEEDORES_INTERVALO_HALF_OPEN_S", cls.intervalo_half_open_s)
            ),
            ttl_open_finance_s=int(
                e.get("PROVEEDORES_TTL_OPEN_FINANCE_S", cls.ttl_open_finance_s)
            ),
            ttl_open_data_s=int(e.get("PROVEEDORES_TTL_OPEN_DATA_S", cls.ttl_open_data_s)),
            jitter_min=float(e.get("PROVEEDORES_JITTER_MIN", cls.jitter_min)),
            jitter_max=float(e.get("PROVEEDORES_JITTER_MAX", cls.jitter_max)),
            ttl_respaldo_s=int(e.get("PROVEEDORES_TTL_RESPALDO_S", cls.ttl_respaldo_s)),
            redis_url=e.get("REDIS_URL"),
        )
