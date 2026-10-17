"""Normalización de los perfiles de datos externos al esquema canónico (HU10).

Es el único módulo que conoce los nombres de campo de cada proveedor (criterio 7,
HA06): lo que sale de aquí usa solo nombres canónicos. Todas las funciones son
puras y deterministas; el reloj no interviene (la fecha de obtención viene en el
resultado de cada fuente).

SUPUESTOS DE PROTOTIPO (a confirmar con negocio): los rangos válidos de
`CAMPOS` y la prioridad Open Finance > Open Data en conflictos.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.proveedores.dominio import Fuente, PerfilCrudo, ResultadoFuente

log = logging.getLogger(__name__)


class MotivoAusencia(str, Enum):
    """Por qué un campo canónico no está en el perfil (nunca se inventa un valor)."""

    NO_REPORTADO = "no_reportado"  # ninguna fuente lo trajo
    SIN_DATOS = "sin_datos"  # la fuente respondió con el perfil por defecto
    FUERA_DE_RANGO = "fuera_de_rango"
    INVALIDO = "invalido"  # tipo o formato no esperado
    DEGRADADO = "degradado"  # solo disponible desde respaldo (dato viejo)
    SIN_CONSENTIMIENTO = "sin_consentimiento"


@dataclass(frozen=True)
class EspecCampo:
    """Definición de un campo del esquema canónico."""

    nombre: str
    unidad: str
    minimo: float
    maximo: float
    entero: bool = False


# Esquema canónico: los campos que consume el motor de rating (HU12).
CAMPOS: dict[str, EspecCampo] = {
    c.nombre: c
    for c in (
        EspecCampo("ingresos_mensuales_cop", "COP", 0, 1_000_000_000),
        EspecCampo("ratio_endeudamiento", "fraccion", 0, 1),
        EspecCampo("antiguedad_cuentas_anios", "anios", 0, 80),
        EspecCampo("estrato", "nivel", 1, 6, entero=True),
        EspecCampo("reportes_negativos", "conteo", 0, 1000, entero=True),
    )
}

# Prioridad ante conflicto: primero gana (supuesto inicial, a confirmar).
PRIORIDAD: tuple[Fuente, ...] = (Fuente.OPEN_FINANCE, Fuente.OPEN_DATA)

# campo crudo del proveedor -> campo canónico. Aquí, y solo aquí, viven los
# nombres del contrato de cada proveedor.
Normalizador = Callable[[Mapping[str, Any]], dict[str, Any]]


def _mapeo(equivalencias: Mapping[str, str]) -> Normalizador:
    def normalizar(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            canonico: payload[crudo]
            for crudo, canonico in equivalencias.items()
            if crudo in payload
        }

    return normalizar


NORMALIZADORES: dict[Fuente, Normalizador] = {
    Fuente.OPEN_FINANCE: _mapeo(
        {
            "monthlyIncomeCop": "ingresos_mensuales_cop",
            "debtRatio": "ratio_endeudamiento",
            "accountsOpenYears": "antiguedad_cuentas_anios",
        }
    ),
    Fuente.OPEN_DATA: _mapeo(
        {"estrato": "estrato", "reportes_negativos": "reportes_negativos"}
    ),
}


@dataclass(frozen=True)
class CampoPerfil:
    valor: int | float
    fuente: Fuente
    obtenido_en: datetime | None = None


@dataclass(frozen=True)
class Conflicto:
    campo: str
    ganadora: Fuente
    descartadas: tuple[Fuente, ...]


@dataclass(frozen=True)
class PerfilCanonico:
    cliente_id: str
    campos: Mapping[str, CampoPerfil] = field(default_factory=dict)
    ausentes: Mapping[str, MotivoAusencia] = field(default_factory=dict)
    conflictos: tuple[Conflicto, ...] = ()
    degradado: bool = False
    con_consentimiento: bool = True
    hit_cache: bool = False
    fuentes: tuple[str, ...] = ()

    @property
    def completitud(self) -> float:
        """Fracción de campos canónicos presentes, entre 0 y 1."""
        return round(len(self.campos) / len(CAMPOS), 4)

    def a_dict(self) -> dict[str, Any]:
        """Forma serializable (fechas en ISO 8601) para cachear o publicar."""
        return {
            "cliente_id": self.cliente_id,
            "campos": {
                n: {
                    "valor": c.valor,
                    "fuente": c.fuente.value,
                    "obtenido_en": c.obtenido_en.isoformat() if c.obtenido_en else None,
                }
                for n, c in sorted(self.campos.items())
            },
            "ausentes": {n: m.value for n, m in sorted(self.ausentes.items())},
            "conflictos": [
                {
                    "campo": c.campo,
                    "ganadora": c.ganadora.value,
                    "descartadas": [f.value for f in c.descartadas],
                }
                for c in self.conflictos
            ],
            "completitud": self.completitud,
            "degradado": self.degradado,
            "con_consentimiento": self.con_consentimiento,
            "hit_cache": self.hit_cache,
            "fuentes": list(self.fuentes),
        }


def validar(espec: EspecCampo, bruto: Any) -> int | float | MotivoAusencia:
    """Devuelve el valor normalizado o el motivo por el que se descarta."""
    if isinstance(bruto, bool) or not isinstance(bruto, int | float):
        return MotivoAusencia.INVALIDO
    if isinstance(bruto, float) and not math.isfinite(bruto):
        return MotivoAusencia.INVALIDO
    if not espec.minimo <= bruto <= espec.maximo:
        return MotivoAusencia.FUERA_DE_RANGO
    if espec.entero:
        if bruto != int(bruto):
            return MotivoAusencia.INVALIDO
        return int(bruto)
    return bruto


def _acumular(
    resultado: ResultadoFuente,
    normalizadores: Mapping[Fuente, Normalizador],
    validos: dict[str, list[CampoPerfil]],
    descartados: dict[str, MotivoAusencia],
) -> None:
    normalizar = normalizadores.get(resultado.fuente)
    if normalizar is None:
        return
    if resultado.por_defecto:
        # Perfil por defecto de HU09: significa "sin datos", nunca un dato real.
        for nombre in CAMPOS:
            descartados.setdefault(nombre, MotivoAusencia.SIN_DATOS)
        return
    for nombre, bruto in normalizar(resultado.payload).items():
        espec = CAMPOS.get(nombre)
        if espec is None:
            continue
        if resultado.degradado:
            # El respaldo es un dato viejo: se conserva la marca, no el valor.
            descartados.setdefault(nombre, MotivoAusencia.DEGRADADO)
            continue
        valor = validar(espec, bruto)
        if isinstance(valor, MotivoAusencia):
            # Se registra el campo y el motivo, nunca el valor (dato financiero).
            log.warning("campo %s descartado (%s)", nombre, valor.value)
            descartados.setdefault(nombre, valor)
            continue
        validos.setdefault(nombre, []).append(
            CampoPerfil(valor, resultado.fuente, resultado.obtenido_en)
        )


def _resolver(
    nombre: str, candidatos: list[CampoPerfil], prioridad: tuple[Fuente, ...]
) -> tuple[CampoPerfil, Conflicto | None]:
    orden = {f: i for i, f in enumerate(prioridad)}
    ordenados = sorted(candidatos, key=lambda c: orden.get(c.fuente, len(orden)))
    ganador = ordenados[0]
    distintos = [c for c in ordenados[1:] if c.valor != ganador.valor]
    if not distintos:
        return ganador, None
    return ganador, Conflicto(
        campo=nombre,
        ganadora=ganador.fuente,
        descartadas=tuple(c.fuente for c in distintos),
    )


def normalizar(
    crudo: PerfilCrudo,
    *,
    normalizadores: Mapping[Fuente, Normalizador] = NORMALIZADORES,
    prioridad: tuple[Fuente, ...] = PRIORIDAD,
) -> PerfilCanonico:
    """Convierte el perfil crudo de HU09 en el perfil canónico (HU10)."""
    if crudo.por_defecto and not crudo.fuentes:
        # ServicioPerfil responde así solo cuando no hay consentimiento.
        return PerfilCanonico(
            cliente_id=crudo.cliente_id,
            ausentes=dict.fromkeys(CAMPOS, MotivoAusencia.SIN_CONSENTIMIENTO),
            con_consentimiento=False,
        )
    validos: dict[str, list[CampoPerfil]] = {}
    descartados: dict[str, MotivoAusencia] = {}
    for resultado in crudo.fuentes:
        _acumular(resultado, normalizadores, validos, descartados)
    campos: dict[str, CampoPerfil] = {}
    conflictos: list[Conflicto] = []
    for nombre in CAMPOS:
        if nombre in validos:
            campo, conflicto = _resolver(nombre, validos[nombre], prioridad)
            campos[nombre] = campo
            if conflicto is not None:
                conflictos.append(conflicto)
    ausentes = {
        n: descartados.get(n, MotivoAusencia.NO_REPORTADO)
        for n in CAMPOS
        if n not in campos
    }
    return PerfilCanonico(
        cliente_id=crudo.cliente_id,
        campos=campos,
        ausentes=ausentes,
        conflictos=tuple(conflictos),
        degradado=crudo.degradado,
        hit_cache=bool(crudo.fuentes) and all(f.hit_cache for f in crudo.fuentes),
        fuentes=tuple(f.fuente.value for f in crudo.fuentes),
    )
