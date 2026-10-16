"""Parámetros de la verificación KYC: configuración, no constantes en código (HU02)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigKyc:
    timeout_s: float = 5.0  # timeout duro del adaptador; al excederlo el caso queda EN_REVISION
    max_intentos: int = 3  # verificaciones rechazadas permitidas dentro de la ventana de bloqueo
    bloqueo_min: int = 30  # ventana (y duración) del bloqueo temporal tras agotar los intentos
    max_bytes_imagen: int = 5_000_000
    exigir_https: bool = False  # en el clúster el borde termina TLS y envía X-Forwarded-Proto
    bucket_evidencias: str | None = None
    directorio_local: str = ".evidencias-kyc"

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigKyc:
        e = os.environ if env is None else env
        return cls(
            timeout_s=float(e.get("KYC_TIMEOUT_S", cls.timeout_s)),
            max_intentos=int(e.get("KYC_MAX_INTENTOS", cls.max_intentos)),
            bloqueo_min=int(e.get("KYC_BLOQUEO_MIN", cls.bloqueo_min)),
            max_bytes_imagen=int(e.get("KYC_MAX_BYTES_IMAGEN", cls.max_bytes_imagen)),
            exigir_https=e.get("KYC_EXIGIR_HTTPS", "false").lower() == "true",
            bucket_evidencias=e.get("KYC_EVIDENCIAS_BUCKET") or None,
            directorio_local=e.get("KYC_EVIDENCIAS_DIR", cls.directorio_local),
        )
