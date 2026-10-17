"""Modelos de dominio del motor de rating (HU12). Sin dependencias de infraestructura."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

MONEDA = "COP"
ESTADO_EMITIDA = "EMITIDA"
AVISO_APROXIMADA = "Este valor es aproximado y puede cambiar al momento de la compra."

# Conceptos del desglose; su suma es exactamente la prima anual.
PRIMA_BASE = "prima_base"
AJUSTE_DATOS = "ajuste_datos"
AJUSTE_RIESGO = "ajuste_riesgo"
RECARGO_DEGRADADO = "recargo_degradado"

# Versión del formato del desglose persistido (HU14); sube si cambia su estructura.
VERSION_DESGLOSE = 1
ETAPA_DATO = "dato"
ETAPA_RIESGO_BASE = "riesgo_base"
ETAPA_SENAL = "senal"


@dataclass(frozen=True)
class PerfilUnificado:
    """Contrato del perfil que consume el motor, derivado del perfil canónico (HU10).

    `senales` usa nombres canónicos (p. ej. `ratio_endeudamiento`). Estará vacío
    cuando no haya consentimiento o el perfil sea degradado. `completitud` es la
    fracción de campos canónicos presentes (0 a 1).
    """

    cliente_id: str
    senales: Mapping[str, int | float] = field(default_factory=dict)
    degradado: bool = False
    con_consentimiento: bool = True
    hit_cache: bool = False
    fuentes: tuple[str, ...] = ()
    completitud: float = 0.0
    # campo canónico -> (fuente, fecha de obtención); solo referencias, sin valores
    procedencia: Mapping[str, tuple[str, datetime | None]] = field(default_factory=dict)


@dataclass(frozen=True)
class Componente:
    concepto: str
    monto: Decimal


@dataclass(frozen=True)
class CoberturaCalculada:
    tipo: str
    monto: Decimal
    deducible: Decimal


@dataclass(frozen=True)
class FactorAplicado:
    """Un factor multiplicativo del cálculo. Las señales citan su procedencia."""

    etapa: str
    nombre: str
    factor: Decimal
    fuente: str | None = None
    obtenido_en: datetime | None = None


@dataclass(frozen=True)
class PerfilUsado:
    """Referencia al perfil que alimentó el cálculo: sin valores personales (HU14)."""

    fuentes: tuple[str, ...]
    degradado: bool
    con_consentimiento: bool
    completitud: float


@dataclass(frozen=True)
class DesglosePrecio:
    """Cómo se calculó una prima, suficiente para explicarla y reproducirla.

    prima_anual = r(r(r(prima_base * Πfactores de dato) * factor de riesgo) * (1 + recargo))
    donde el factor de riesgo es `riesgo_base` por el producto de las señales.
    """

    version_esquema: int
    version_reglas: str
    producto: str
    moneda: str
    prima_base: Decimal
    factores: tuple[FactorAplicado, ...]
    recargo_degradado_pct: Decimal | None
    componentes: tuple[Componente, ...]
    prima_anual: Decimal
    perfil: PerfilUsado


@dataclass(frozen=True)
class ResultadoRating:
    producto: str
    moneda: str
    prima_anual: Decimal
    prima_mensual: Decimal
    coberturas: tuple[CoberturaCalculada, ...]
    componentes: tuple[Componente, ...]
    version_reglas: str
    vigencia_dias: int
    degradado: bool
    aproximada: bool
    desglose: DesglosePrecio

    @property
    def aviso(self) -> str | None:
        return AVISO_APROXIMADA if self.aproximada else None


@dataclass(frozen=True)
class SolicitudCotizacion:
    cliente_id: str
    producto: str
    datos: Mapping[str, Any]


@dataclass(frozen=True)
class CotizacionEmitida:
    """Cotización ya persistida, tal como se entrega a los canales.

    `componentes` y `desglose` salen del desglose persistido (HU14); en
    cotizaciones anteriores a HU14 vienen vacíos.
    """

    id: str
    cliente_id: str
    producto: str
    estado: str
    moneda: str
    prima_anual: Decimal
    prima_mensual: Decimal
    coberturas: tuple[CoberturaCalculada, ...]
    componentes: tuple[Componente, ...]
    version_reglas: str
    degradado: bool
    aproximada: bool
    vigente_hasta: datetime
    creada_en: datetime
    hit_cache: bool = False
    latencia_interna_ms: float | None = None
    desglose: DesglosePrecio | None = None

    @property
    def aviso(self) -> str | None:
        return AVISO_APROXIMADA if self.aproximada else None
