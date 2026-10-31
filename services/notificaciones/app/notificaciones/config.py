"""Configuración de MS Notificaciones desde variables de entorno.

La conexión a Kafka (`KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD`) la lee `app/eventos` (plantilla HS2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./notificaciones.db"
TOPIC_CICLO_VIDA = "solventa.polizas.ciclovida"
# El grupo de consumo debe empezar por el nombre del servicio: es lo único que permite su política de IAM.
GRUPO_POLIZAS = "solventa-notificaciones.polizas"
CORREO_PROVEEDORES = ("simulado", "ses")


@dataclass(frozen=True)
class ConfigNotificaciones:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    kafka_brokers: str | None = None
    identidad_url: str | None = None
    identidad_key: str = ""
    suscripcion_url: str | None = None
    suscripcion_key: str = ""
    timeout_s: float = 3.0
    # Base pública de los enlaces del correo (CloudFront). Sin ella el correo se envía sin el enlace de descarga.
    url_publica: str | None = None
    # `simulado` no envía nada; `ses` usa Amazon SES (la cuenta en sandbox solo envía a direcciones verificadas).
    correo_proveedor: str = "simulado"
    correo_remitente: str | None = None
    correo_region: str | None = None
    # Reintentos del envío: espera creciente; agotados, el envío queda FALLIDO y visible al operador.
    max_intentos: int = 5
    reintento_base_s: float = 5.0
    reintento_maximo_s: float = 300.0
    # JWT del operador emitido por Identidad (ADR-0008); solo protege la lista de envíos fallidos.
    asesor_jwt_secreto: str | None = None
    asesor_jwt_emisor: str = "solventa-identidad"

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigNotificaciones:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("NOTIFICACIONES_DATABASE_URL") or e.get("DATABASE_URL"),
            kafka_brokers=e.get("KAFKA_BOOTSTRAP_SERVERS") or None,
            identidad_url=e.get("IDENTIDAD_URL") or None,
            identidad_key=e.get("IDENTIDAD_SERVICE_KEY", ""),
            suscripcion_url=e.get("SUSCRIPCION_URL") or None,
            suscripcion_key=e.get("SUSCRIPCION_SERVICE_KEY", ""),
            timeout_s=float(e.get("NOTIFICACIONES_TIMEOUT_S", "3")),
            url_publica=(e.get("URL_PUBLICA") or "").rstrip("/") or None,
            correo_proveedor=e.get("CORREO_PROVEEDOR", cls.correo_proveedor).strip().lower(),
            correo_remitente=e.get("CORREO_REMITENTE") or None,
            correo_region=e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or None,
            max_intentos=int(e.get("NOTIFICACIONES_MAX_INTENTOS", "5")),
            reintento_base_s=float(e.get("NOTIFICACIONES_REINTENTO_BASE_S", "5")),
            reintento_maximo_s=float(e.get("NOTIFICACIONES_REINTENTO_MAXIMO_S", "300")),
            asesor_jwt_secreto=e.get("ASESOR_JWT_SECRETO") or None,
            asesor_jwt_emisor=e.get("ASESOR_JWT_EMISOR", cls.asesor_jwt_emisor),
        )
