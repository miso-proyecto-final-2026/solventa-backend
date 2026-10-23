"""Dominio de MS Suscripción: estados de la saga, perfil de riesgo y resultado de la decisión."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EstadoSaga(StrEnum):
    """Estados que expone `GET /v1/suscripciones/{id}` (documentación técnica §1.5)."""

    EN_PROGRESO = "EN_PROGRESO"
    APROBADA = "APROBADA"
    COBRO_REALIZADO = "COBRO_REALIZADO"
    FIRMADA = "FIRMADA"
    EMITIDA = "EMITIDA"
    RECHAZADA = "RECHAZADA"
    COMPENSADA = "COMPENSADA"


class PasoSaga(StrEnum):
    DECISION = "DECISION"
    COBRO = "COBRO"
    FIRMA = "FIRMA"
    EMISION = "EMISION"


PASOS = [p.value for p in PasoSaga]


class Decision(StrEnum):
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"
    AJUSTADA = "AJUSTADA"
    DERIVADA = "DERIVADA"


@dataclass(frozen=True)
class PerfilRiesgo:
    """Lo que la decisión necesita del perfil que entrega MS Perfilamiento."""

    disponible: bool
    con_consentimiento: bool
    degradado: bool
    completitud: float
    campos: dict[str, float] = field(default_factory=dict)
    motivo_indisponible: str | None = None

    @classmethod
    def no_disponible(cls, motivo: str) -> PerfilRiesgo:
        return cls(disponible=False, con_consentimiento=False, degradado=True, completitud=0.0, motivo_indisponible=motivo)

    @classmethod
    def sin_consentimiento(cls) -> PerfilRiesgo:
        return cls(disponible=True, con_consentimiento=False, degradado=True, completitud=0.0)


@dataclass(frozen=True)
class SolicitudDecision:
    producto: str
    monto_asegurado: int
    perfil: PerfilRiesgo


@dataclass(frozen=True)
class ResultadoDecision:
    decision: Decision
    motivos: list[str]
    ajuste: dict[str, Any] | None
    reglas_version: str
