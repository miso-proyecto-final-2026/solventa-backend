"""Clasificación de las señales del perfil en niveles de riesgo para el asesor (HU38).

Estrategia dirigida por datos: los cortes viven en `reglas/niveles_perfil.yaml`, no en el código.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.cotizacion.errores import ErrorReglas

RUTA_NIVELES = Path(__file__).parent / "reglas" / "niveles_perfil.yaml"

BAJO, MEDIO, ALTO = "BAJO", "MEDIO", "ALTO"


class UmbralSenal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sentido: Literal["directo", "inverso"]
    medio: float
    alto: float


class ClasificadorSenales:
    def __init__(self, umbrales: Mapping[str, UmbralSenal]) -> None:
        self._umbrales = dict(umbrales)

    @classmethod
    def desde_archivo(cls, ruta: Path = RUTA_NIVELES) -> ClasificadorSenales:
        try:
            crudo = yaml.safe_load(ruta.read_text(encoding="utf-8"))
            umbrales = {
                nombre: UmbralSenal.model_validate(u)
                for nombre, u in crudo["senales"].items()
            }
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise ErrorReglas(
                f"niveles de perfil inválidos ({ruta.name}): {exc}"
            ) from exc
        return cls(umbrales)

    def clasificar(self, nombre: str, valor: float) -> str | None:
        """Nivel de la señal, o None si no hay cortes definidos para ella."""
        u = self._umbrales.get(nombre)
        if u is None:
            return None
        if u.sentido == "directo":
            return ALTO if valor >= u.alto else MEDIO if valor >= u.medio else BAJO
        return ALTO if valor < u.alto else MEDIO if valor < u.medio else BAJO

    def clasificar_todas(
        self, senales: Mapping[str, int | float]
    ) -> list[tuple[str, str]]:
        """Pares (señal, nivel) en el orden de los cortes; ignora señales sin cortes."""
        return [
            (nombre, self.clasificar(nombre, senales[nombre]) or BAJO)
            for nombre in self._umbrales
            if nombre in senales
        ]
