"""Modelos y puertos de dominio de la integración con datos externos (HU23, HA06).

Réplica deliberada de `solventa-backend/services/cotizacion/app/proveedores/dominio.py` (HU09):
los servicios no comparten código (ADR-0001) y evolucionan por separado. El resto del servicio
depende de estos puertos; el mapeo al contrato de cada proveedor vive en su adaptador.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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


# Valor por defecto documentado: sin datos externos disponibles. La normalización lo interpreta
# como "campo ausente", nunca como un dato real del cliente.
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
    obtenido_en: datetime | None = None  # cuándo se consultó al proveedor
    ttl_s: int | None = None  # vigencia base configurada para el tipo de dato de la fuente
    timeout: bool = False  # la degradación se debió a que el proveedor no respondió a tiempo


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
    """Métrica por llamada. Sin payload ni datos personales."""

    cliente_id: str
    fuente: Fuente
    latencia_ms: float
    hit_cache: bool
    degradado: bool
    estado_circuito: EstadoCircuito


@dataclass(frozen=True)
class VerificacionConsentimiento:
    estado: EstadoConsentimiento
    consentimiento_id: str | None = None


class ProveedorExterno(Protocol):
    """Lo que acepta `ServicioFuentes` como adaptador de una fuente (HA06, ADR-0004)."""

    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]: ...

    async def sondear(self) -> None: ...


class ConsentimientoPort(Protocol):
    """Contrato mínimo con MS Identidad (HU06). `verificar` devuelve también el id verificado."""

    async def verificar(self, cliente_id: str) -> VerificacionConsentimiento: ...
