"""Proveedor de KYC simulado (HU02). El real no se usa; se simulan latencia y fallos.

`ProveedorKycSimulado` imita el contrato propio del proveedor (`match`, `liveness`, `case_id`) y
`VerificadorSimulado` es el adaptador que lo traduce al dominio: ese mapeo es lo único que cambiaría
al integrar un proveedor real (HA06).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.kyc.dominio import (
    RETO_VIDA,
    DecisionProveedor,
    ProveedorKycError,
    ResultadoVerificacion,
    SolicitudVerificacion,
)


class ModoSimulador(str, Enum):
    EXITO = "exito"
    RECHAZO = "rechazo"
    TIMEOUT = "timeout"
    ERROR_5XX = "error_5xx"


@dataclass(frozen=True)
class ConfigSimulador:
    modo: ModoSimulador = ModoSimulador.EXITO
    latencia_s: float = 0.0
    latencia_timeout_s: float = 3600.0  # en modo TIMEOUT: tarda "para siempre"

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigSimulador:
        e = os.environ if env is None else env
        return cls(
            modo=ModoSimulador(e.get("KYC_SIMULADOR_MODO", cls.modo.value)),
            latencia_s=float(e.get("KYC_SIMULADOR_LATENCIA_S", cls.latencia_s)),
        )


class ProveedorKycSimulado:
    """Servicio externo simulado. Cada clave de idempotencia abre a lo sumo un caso."""

    def __init__(self, config: ConfigSimulador | None = None) -> None:
        self.config = config or ConfigSimulador()
        self._casos: dict[str, str] = {}
        self.llamadas = 0

    @property
    def casos_abiertos(self) -> int:
        return len(self._casos)

    async def crear_caso(self, clave: str, reto: str) -> dict[str, Any]:
        self.llamadas += 1
        if self.config.modo is ModoSimulador.TIMEOUT:
            await asyncio.sleep(self.config.latencia_timeout_s)
        if self.config.latencia_s:
            await asyncio.sleep(self.config.latencia_s)
        if self.config.modo is ModoSimulador.ERROR_5XX:
            raise ConnectionError("503 Service Unavailable")
        case_id = self._casos.setdefault(clave, f"sim-{uuid.uuid4()}")
        exito = self.config.modo is ModoSimulador.EXITO
        return {
            "case_id": case_id,
            "match": exito,
            "liveness": {"challenge": reto, "passed": exito and reto == RETO_VIDA},
        }


class VerificadorSimulado:
    """Adaptador de `VerificadorIdentidad` sobre el proveedor simulado."""

    def __init__(self, proveedor: ProveedorKycSimulado | None = None) -> None:
        self.proveedor = proveedor or ProveedorKycSimulado(ConfigSimulador.desde_entorno())

    async def verificar(self, solicitud: SolicitudVerificacion) -> ResultadoVerificacion:
        try:
            crudo = await self.proveedor.crear_caso(solicitud.clave_idempotencia, solicitud.reto)
        except (ConnectionError, OSError) as exc:
            raise ProveedorKycError("El proveedor de KYC no respondió") from exc
        return self._mapear(crudo)

    @staticmethod
    def _mapear(crudo: dict[str, Any]) -> ResultadoVerificacion:
        try:
            aprobada = bool(crudo["match"]) and bool(crudo["liveness"]["passed"])
            referencia = str(crudo["case_id"])
        except (KeyError, TypeError) as exc:
            raise ProveedorKycError("Respuesta inválida del proveedor de KYC") from exc
        decision = DecisionProveedor.APROBADA if aprobada else DecisionProveedor.RECHAZADA
        return ResultadoVerificacion(decision=decision, referencia=referencia)
