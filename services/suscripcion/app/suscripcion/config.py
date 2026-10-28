"""Configuración de MS Suscripción desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./suscripcion.db"
TOPIC_COMANDOS = "solventa.suscripcion.comandos"
TOPIC_REVISION = "solventa.suscripcion.revision"
TOPIC_RESULTADOS_PAGOS = "solventa.pagos.resultados"
TOPIC_CICLO_VIDA = "solventa.polizas.ciclovida"
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
    # Documento de la póliza (HU20). Con bucket se guarda en S3; sin él, solo en desarrollo, en una carpeta local.
    polizas_bucket: str | None = None
    polizas_documentos_dir: str = "./polizas-local"
    polizas_vigencia_meses: int = 12  # provisional: ningún documento define la vigencia
    polizas_max_intentos: int = 5
    polizas_reintento_base_s: float = 5.0
    polizas_reintento_maximo_s: float = 300.0

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
            polizas_bucket=e.get("POLIZAS_BUCKET") or None,
            polizas_documentos_dir=e.get("POLIZAS_DOCUMENTOS_DIR") or "./polizas-local",
            polizas_vigencia_meses=int(e.get("POLIZAS_VIGENCIA_MESES", "12")),
            polizas_max_intentos=int(e.get("POLIZAS_MAX_INTENTOS", "5")),
            polizas_reintento_base_s=float(e.get("POLIZAS_REINTENTO_BASE_S", "5")),
            polizas_reintento_maximo_s=float(e.get("POLIZAS_REINTENTO_MAXIMO_S", "300")),
        )
