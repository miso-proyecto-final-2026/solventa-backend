"""Reglas de decisión versionadas (EC-M1.2): los umbrales viven en `reglas/<version>.yaml`, no en el código."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DIRECTORIO = Path(__file__).resolve().parents[2] / "reglas"


@dataclass(frozen=True)
class Reglas:
    version: str
    provisional: bool
    campos_requeridos: tuple[str, ...]
    completitud_minima: float
    monto_aprobacion_automatica: int
    monto_maximo_automatico: int
    monto_ajustado_a: int
    endeudamiento_aprobacion: float
    endeudamiento_maximo_con_ajuste: float
    reportes_aprobacion: int
    reportes_maximo_derivable: int

    def __post_init__(self) -> None:
        if not (0 <= self.endeudamiento_aprobacion <= self.endeudamiento_maximo_con_ajuste <= 1):
            raise ValueError("endeudamiento: se espera 0 <= aprobacion <= maximo_con_ajuste <= 1")
        if not (0 < self.monto_ajustado_a <= self.monto_aprobacion_automatica <= self.monto_maximo_automatico):
            raise ValueError("monto: se espera 0 < ajustado_a <= aprobacion_automatica <= maximo_automatico")
        if not (0 <= self.reportes_aprobacion <= self.reportes_maximo_derivable):
            raise ValueError("reportes_negativos: se espera 0 <= aprobacion <= maximo_derivable")
        if not (0 < self.completitud_minima <= 1):
            raise ValueError("completitud_minima debe estar entre 0 y 1")


def cargar_reglas(version: str = "v1", directorio: Path | None = None) -> Reglas:
    archivo = (directorio or DIRECTORIO) / f"{version}.yaml"
    if not archivo.is_file():
        raise FileNotFoundError(f"No existe el archivo de reglas {archivo.name} (versión {version})")
    d = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    return Reglas(
        version=str(d["version"]),
        provisional=bool(d.get("provisional", False)),
        campos_requeridos=tuple(d["perfil"]["campos_requeridos"]),
        completitud_minima=float(d["perfil"]["completitud_minima"]),
        monto_aprobacion_automatica=int(d["monto"]["aprobacion_automatica"]),
        monto_maximo_automatico=int(d["monto"]["maximo_automatico"]),
        monto_ajustado_a=int(d["monto"]["ajustado_a"]),
        endeudamiento_aprobacion=float(d["endeudamiento"]["aprobacion"]),
        endeudamiento_maximo_con_ajuste=float(d["endeudamiento"]["maximo_con_ajuste"]),
        reportes_aprobacion=int(d["reportes_negativos"]["aprobacion"]),
        reportes_maximo_derivable=int(d["reportes_negativos"]["maximo_derivable"]),
    )
