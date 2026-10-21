"""Adaptadores simulados de Open Finance y Open Data (HU09, criterio 8).

Latencia por rango y fallos forzables por configuración, sin cambiar código.
Cada adaptador concentra el mapeo al contrato de su proveedor (HA06).
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.proveedores.demo import CatalogoPerfilesDemo
from app.proveedores.dominio import ProveedorError


class ModoFalla(str, Enum):
    NINGUNO = "ninguno"
    TIMEOUT = "timeout"
    ERROR_5XX = "error_5xx"
    VACIO = "vacio"


@dataclass(frozen=True)
class ConfigSimulador:
    latencia_min_ms: float = 20.0
    latencia_max_ms: float = 80.0
    modo: ModoFalla = ModoFalla.NINGUNO

    @classmethod
    def desde_entorno(
        cls, prefijo: str, env: dict[str, str] | None = None
    ) -> ConfigSimulador:
        e = os.environ if env is None else env
        return cls(
            latencia_min_ms=float(
                e.get(f"{prefijo}_LATENCIA_MIN_MS", cls.latencia_min_ms)
            ),
            latencia_max_ms=float(
                e.get(f"{prefijo}_LATENCIA_MAX_MS", cls.latencia_max_ms)
            ),
            modo=ModoFalla(e.get(f"{prefijo}_MODO", ModoFalla.NINGUNO.value)),
        )


class _SimuladorBase:
    def __init__(
        self,
        config: ConfigSimulador | None = None,
        rng: random.Random | None = None,
        perfiles_demo: CatalogoPerfilesDemo | None = None,
    ):
        self.config = config or ConfigSimulador()
        self._rng = rng or random.Random()
        self._demo = perfiles_demo

    async def _simular(self) -> bool:
        """Aplica latencia/fallo. Devuelve False si la respuesta debe ser vacía."""
        modo = self.config.modo
        if modo is ModoFalla.TIMEOUT:
            await asyncio.sleep(3600)  # el llamador corta con su timeout duro
        espera_ms = self._rng.uniform(
            self.config.latencia_min_ms, self.config.latencia_max_ms
        )
        await asyncio.sleep(espera_ms / 1000)
        if modo is ModoFalla.ERROR_5XX:
            raise ProveedorError("simulador: 503 Service Unavailable")
        return modo is not ModoFalla.VACIO

    async def sondear(self) -> None:
        """Sondeo de salud (half-open): mismo comportamiento de fallo, sin datos."""
        if not await self._simular():
            raise ProveedorError("simulador: respuesta vacía")


class SimuladorOpenFinance(_SimuladorBase):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]:
        if not await self._simular():
            return {}
        # Contrato propio del proveedor simulado; HU10 lo mapea al esquema canónico.
        asignado = self._demo.para(cliente_id, "open_finance") if self._demo else None
        if asignado is not None:
            return {"customerId": cliente_id, **asignado}
        return {
            "customerId": cliente_id,
            "monthlyIncomeCop": 4_500_000,
            "debtRatio": 0.32,
            "accountsOpenYears": 6,
        }


class SimuladorOpenData(_SimuladorBase):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]:
        if not await self._simular():
            return {}
        asignado = self._demo.para(cliente_id, "open_data") if self._demo else None
        if asignado is not None:
            return {"documento_ref": cliente_id, **asignado}
        return {
            "documento_ref": cliente_id,
            "ocupacion": "empleado",
            "estrato": 3,
            "reportes_negativos": 0,
        }
