"""Revisión asistida (HU16): reglas puras de la bandeja del asesor.

Sin base de datos ni red: categorizar el motivo de la derivación, calcular la prioridad por antigüedad y ajustar la cobertura.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.suscripcion.errores import AjusteInvalido


class CategoriaMotivo(StrEnum):
    PERFIL_FUERA_DE_RANGO = "PERFIL_FUERA_DE_RANGO"  # «Registra N reporte(s) negativo(s)…»
    MONTO_SOBRE_LIMITE = "MONTO_SOBRE_LIMITE"  # «El monto asegurado (…) supera el límite…»
    # Perfil no disponible, sin consentimiento, incompleto o con un dato faltante. HU15 no tiene una regla de inconsistencia real.
    DATOS_INCOMPLETOS_O_INCONSISTENTES = "DATOS_INCOMPLETOS_O_INCONSISTENTES"


class Prioridad(StrEnum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"


class MotivoSinCategoria(ValueError):
    pass


_RANGO = ("reporte(s) negativo(s)",)
_MONTO = ("supera el límite de aprobación automática",)
_DATOS = (
    "perfil del cliente no está disponible",
    "consentimiento vigente",
    "perfil está incompleto",
    "Falta el dato requerido",
)


def categorizar(motivo: str, *, estricto: bool = False) -> CategoriaMotivo:
    """Las tres categorías de la guía. Un motivo desconocido cae en datos (lo más prudente); `estricto` lo convierte en error,
    para que una prueba con los mensajes reales de `decision.py` falle si aparece uno nuevo sin categoría."""
    if any(t in motivo for t in _RANGO):
        return CategoriaMotivo.PERFIL_FUERA_DE_RANGO
    if any(t in motivo for t in _MONTO):
        return CategoriaMotivo.MONTO_SOBRE_LIMITE
    if any(t in motivo for t in _DATOS):
        return CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES
    if estricto:
        raise MotivoSinCategoria(motivo)
    return CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES


def categorias(motivos: list[str]) -> list[CategoriaMotivo]:
    """Las categorías distintas de la lista, en el orden en que aparecen."""
    return list(dict.fromkeys(categorizar(m) for m in motivos))


@dataclass(frozen=True)
class UmbralesPrioridad:
    alta_horas: int = 24
    media_horas: int = 4


def prioridad(creada_en: datetime, ahora: datetime, umbrales: UmbralesPrioridad = UmbralesPrioridad()) -> Prioridad:
    """ALTA con `alta_horas` o más en la bandeja, MEDIA con `media_horas` o más, BAJA en otro caso."""
    espera = ahora - creada_en
    if espera >= timedelta(hours=umbrales.alta_horas):
        return Prioridad.ALTA
    if espera >= timedelta(hours=umbrales.media_horas):
        return Prioridad.MEDIA
    return Prioridad.BAJA


@dataclass(frozen=True)
class Ajuste:
    factor: float
    monto_original: int
    monto_asegurado: int
    prima_mensual: int
    prima_anual: int
    prima_mensual_original: int
    prima_anual_original: int
    coberturas: list[dict[str, Any]]

    def como_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "monto_original": self.monto_original,
            "monto_asegurado": self.monto_asegurado,
            "prima_mensual_original": self.prima_mensual_original,
            "prima_mensual": self.prima_mensual,
            "prima_anual_original": self.prima_anual_original,
            "prima_anual": self.prima_anual,
        }


def ajustar(
    *,
    monto_asegurado: int,
    prima_mensual: int,
    prima_anual: int,
    coberturas: list[dict[str, Any]] | None,
    nuevo_monto: int,
) -> Ajuste:
    """Baja el monto asegurado y escala coberturas y primas en proporción (enteros COP). El deducible no cambia.

    La prima proporcional es una aproximación provisional: ignora la prima mínima y los factores del motor de Cotización (ADR-0022).
    """
    if nuevo_monto <= 0 or nuevo_monto >= monto_asegurado:
        raise AjusteInvalido
    factor = nuevo_monto / monto_asegurado
    escaladas = [{**c, "monto": round(int(c["monto"]) * factor)} for c in (coberturas or [])]
    return Ajuste(
        factor=round(factor, 6),
        monto_original=monto_asegurado,
        monto_asegurado=nuevo_monto,
        prima_mensual=round(prima_mensual * factor),
        prima_anual=round(prima_anual * factor),
        prima_mensual_original=prima_mensual,
        prima_anual_original=prima_anual,
        coberturas=escaladas,
    )
