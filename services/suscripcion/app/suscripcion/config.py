"""Configuración de MS Suscripción desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./suscripcion.db"
TOPIC_COMANDOS = "solventa.suscripcion.comandos"
TOPIC_REVISION = "solventa.suscripcion.revision"
TOPIC_RESULTADOS_PAGOS = "solventa.pagos.resultados"
# El grupo de consumo debe empezar por el nombre del servicio: es lo único que permite su política de IAM.
GRUPO_PAGOS = "solventa-suscripcion.pagos"


@dataclass(frozen=True)
class ConfigSuscripcion:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    identidad_url: str | None = None
    identidad_key: str = ""
    cotizacion_url: str | None = None
    perfilamiento_url: str | None = None
    perfilamiento_key: str = ""
    timeout_s: float = 2.0
    # Una saga sin decidir tras estos segundos la retoma el barrido (el proceso que la iba a decidir cayó).
    espera_barrido_s: float = 30.0

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigSuscripcion:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("SUSCRIPCION_DATABASE_URL") or e.get("DATABASE_URL"),
            identidad_url=e.get("IDENTIDAD_URL") or None,
            identidad_key=e.get("IDENTIDAD_SERVICE_KEY", ""),
            cotizacion_url=e.get("COTIZACION_URL") or None,
            perfilamiento_url=e.get("PERFILAMIENTO_URL") or None,
            perfilamiento_key=e.get("PERFILAMIENTO_SERVICE_KEY", ""),
            timeout_s=float(e.get("SUSCRIPCION_TIMEOUT_S", "2")),
            espera_barrido_s=float(e.get("SUSCRIPCION_ESPERA_BARRIDO_S", "30")),
        )
