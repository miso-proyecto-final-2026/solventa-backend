"""Conexión a MSK: SASL/IAM con credenciales de EKS Pod Identity (staging) o PLAINTEXT (solo pruebas locales)."""

from __future__ import annotations

import asyncio
import os
import ssl
from collections.abc import Callable
from typing import Any

from aiokafka.abc import AbstractTokenProvider


class ProveedorTokenIam(AbstractTokenProvider):
    """Firma el token de autenticación de MSK con las credenciales del pod.

    La firma puede bloquear un instante (resuelve credenciales), así que corre fuera del bucle de eventos.
    """

    def __init__(self, region: str, firmar: Callable[[str], tuple[str, int]] | None = None) -> None:
        self._region = region
        self._firmar = firmar or self._firmar_con_msk

    @staticmethod
    def _firmar_con_msk(region: str) -> tuple[str, int]:
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        return MSKAuthTokenProvider.generate_auth_token(region)

    async def token(self) -> str:
        token, _expira_en_ms = await asyncio.get_running_loop().run_in_executor(None, self._firmar, self._region)
        return token


def parametros_seguridad(seguridad: str | None = None, region: str | None = None) -> dict[str, Any]:
    """Argumentos de seguridad para productores y consumidores de aiokafka."""
    modo = (seguridad or os.environ.get("KAFKA_SEGURIDAD", "IAM")).upper()
    if modo == "PLAINTEXT":
        return {"security_protocol": "PLAINTEXT"}
    if modo != "IAM":
        raise ValueError(f"KAFKA_SEGURIDAD debe ser IAM o PLAINTEXT, no {modo!r}")
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise ValueError("La autenticación IAM necesita AWS_REGION")
    return {
        "security_protocol": "SASL_SSL",
        # aiokafka exige el contexto TLS con SASL_SSL (sin él ni siquiera construye el cliente): valida certificado y nombre.
        "ssl_context": ssl.create_default_context(),
        "sasl_mechanism": "OAUTHBEARER",
        "sasl_oauth_token_provider": ProveedorTokenIam(region),
    }


def brokers_desde_entorno() -> list[str]:
    """Brokers de `KAFKA_BOOTSTRAP_SERVERS`, que la plataforma inyecta en los servicios con eventos."""
    valor = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    brokers = [b.strip() for b in valor.split(",") if b.strip()]
    if not brokers:
        raise ValueError("Falta KAFKA_BOOTSTRAP_SERVERS (la plataforma lo entrega a los servicios con `eventos`)")
    return brokers
