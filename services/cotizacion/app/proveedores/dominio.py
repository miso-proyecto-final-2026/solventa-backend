"""Modelos y puertos de dominio de la integración con datos externos (HU09, HA06).

El resto del servicio depende de estos puertos; el mapeo al contrato de cada
proveedor vive únicamente en su adaptador.
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


# Categorías de datos que se consultan a cada fuente (trazabilidad, HU11). Supuesto de
# prototipo: se alinean con los campos canónicos que cada fuente alimenta.
CATEGORIAS_POR_FUENTE: dict[Fuente, tuple[str, ...]] = {
    Fuente.OPEN_FINANCE: ("ingresos", "endeudamiento", "antiguedad_financiera"),
    Fuente.OPEN_DATA: ("estrato", "reportes_negativos"),
}


class ResultadoConsulta(str, Enum):
    """Resultado de una consulta a un proveedor, tal como se audita (HU11)."""

    OK = "OK"
    TIMEOUT = "TIMEOUT"
    DEGRADADO = "DEGRADADO"
    SIN_CONSENTIMIENTO = "SIN_CONSENTIMIENTO"


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
    obtenido_en: datetime | None = None  # cuándo se consultó al proveedor
    timeout: bool = (
        False  # la degradación se debió a que el proveedor no respondió a tiempo
    )


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


@dataclass(frozen=True)
class RegistroTrazabilidad:
    """Huella de una consulta a un proveedor: solo metadatos y referencias (HU11).

    Nunca contiene el payload de la respuesta ni datos personales; `cliente_id` y
    `consentimiento_id` son referencias.
    """

    id: str
    cliente_id: str
    proveedor: str
    categorias: tuple[str, ...]
    consultado_en: datetime
    resultado: ResultadoConsulta
    latencia_ms: float
    hit_cache: bool
    consentimiento_id: str | None = None
    cotizacion_id: str | None = None
    correlacion_id: str | None = None


@dataclass(frozen=True)
class VerificacionConsentimiento:
    estado: EstadoConsentimiento
    consentimiento_id: str | None = None


class ProveedorOpenFinance(Protocol):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]: ...

    async def sondear(self) -> None: ...


class ProveedorOpenData(Protocol):
    async def consultar_perfil(self, cliente_id: str) -> dict[str, Any]: ...

    async def sondear(self) -> None: ...


class ConsentimientoPort(Protocol):
    """Contrato mínimo con MS Identidad (HU06). Hasta que exista se usa un stub.

    `verificar` devuelve además el id del consentimiento verificado (HU11).
    """

    async def verificar(self, cliente_id: str) -> VerificacionConsentimiento: ...
