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


@dataclass(frozen=True)
class PerfilUnificado:
    """Contrato mínimo del perfil que consume el motor (HU10 lo producirá a futuro).

    `senales` usa nombres canónicos (p. ej. `ratio_endeudamiento`). Estará vacío
    cuando no haya consentimiento o el perfil sea degradado.
    """

    cliente_id: str
    senales: Mapping[str, int | float] = field(default_factory=dict)
    degradado: bool = False
    con_consentimiento: bool = True
    hit_cache: bool = False
    fuentes: tuple[str, ...] = ()


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

    `componentes` viene vacío al consultarla después: el desglose persistido
    para auditoría es HU14.
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

    @property
    def aviso(self) -> str | None:
        return AVISO_APROXIMADA if self.aproximada else None
