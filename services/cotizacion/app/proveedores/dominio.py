"""Modelos y puertos de dominio de la integración con datos externos (HU09, HA06).

El resto del servicio depende de estos puertos; el mapeo al contrato de cada
proveedor vive únicamente en su adaptador.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class Fuente(str, Enum):
    OPEN_FINANCE = "open_finance"
    OPEN_DATA = "open_data"


class EstadoConsentimiento(str, Enum):
    OTORGADO = "OTORGADO"
    NO_OTORGADO = "NO_OTORGADO"
    REVOCADO = "REVOCADO"


class EstadoCircuito(str, Enum):
    CERRADO = "cerrado"
    ABIERTO = "abierto"
    SEMIABIERTO = "semiabierto"


# Valor por defecto documentado: sin datos externos disponibles. La normalización
# (HU10) lo interpreta como "campo ausente", nunca como un dato real del cliente.
PERFIL_POR_DEFECTO: dict[str, Any] = {"datos_disponibles": False}


class ProveedorError(Exception):
    """Fallo del proveedor externo (5xx, red, respuesta vacía)."""


@dataclass(frozen=True)
class ResultadoFuente:
    """Resultado crudo de una fuente, con su origen y marcas de calidad."""

    fuente: Fuente
    payload: dict[str, Any]
    degradado: bool = False
    por_defecto: bool = False
    hit_cache: bool = False


@dataclass(frozen=True)
class PerfilCrudo:
    cliente_id: str
    fuentes: list[ResultadoFuente] = field(default_factory=list)
    por_defecto: bool = False

    @property
    def degradado(self) -> bool:
        return any(f.degradado for f in self.fuentes)


@dataclass(frozen=True)
class RegistroLlamada:
    """Métrica por llamada (insumo de HU11 y de HA01). Sin payload ni datos personales."""

    cliente_id: str
    fuente: Fuente
    latencia_ms: float
    hit_cache: bool
    degradado: bool
    estado_circuito: EstadoCircuito


class ProveedorOpenFinance(Protocol):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]: ...

    async def sondear(self) -> None: ...


class ProveedorOpenData(Protocol):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]: ...

    async def sondear(self) -> None: ...


class ConsentimientoPort(Protocol):
    """Contrato mínimo con MS Identidad (HU06). Hasta que exista se usa un stub."""

    async def estado(self, cliente_id: str) -> EstadoConsentimiento: ...
