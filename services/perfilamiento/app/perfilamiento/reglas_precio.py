"""Reglas de precio del seguro de vida hipotecario: modelo, validación y carga (HU25).

Las tarifas, factores y recargos viven en `reglas/<archivo>.yaml`, nunca en el código del cálculo. Se validan al
cargar para que una regla mal escrita falle al arrancar y no al ofertar. Mismo estilo que `reglas.py` de
Cotización (ADR-0003); cada servicio evoluciona sus reglas por separado (ADR-0001).
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

DIRECTORIO_REGLAS = Path(__file__).parent / "reglas"
ARCHIVO_POR_DEFECTO = "vida_hipotecario_v1.yaml"


class ErrorReglas(Exception):
    """Las reglas de precio no se pueden cargar o no son coherentes."""


def _a_decimal(valor: object) -> object:
    # Los YAML traen floats (0.0018); se pasan por su repr para no arrastrar ruido binario.
    if isinstance(valor, float):
        return Decimal(repr(valor))
    if isinstance(valor, int) and not isinstance(valor, bool):
        return Decimal(valor)
    return valor


Dec = Annotated[Decimal, BeforeValidator(_a_decimal)]


class _Modelo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Rango(_Modelo):
    """Intervalo [desde, hasta). `None` en un extremo significa sin límite."""

    desde: Dec | None = None
    hasta: Dec | None = None
    factor: Dec = Field(gt=0)

    def contiene(self, valor: Decimal) -> bool:
        return (self.desde is None or valor >= self.desde) and (self.hasta is None or valor < self.hasta)


class RangoEtapa(Rango):
    etapa: str


def _validar_tabla(rangos: list, sentido: str | None, nombre: str) -> None:
    if not rangos:
        raise ValueError(f"{nombre}: la tabla no puede estar vacía")
    if rangos[0].desde is not None or rangos[-1].hasta is not None:
        raise ValueError(f"{nombre}: el primer rango debe tener desde=null y el último hasta=null")
    for anterior, actual in itertools.pairwise(rangos):
        if anterior.hasta is None or actual.desde is None or anterior.hasta != actual.desde:
            raise ValueError(f"{nombre}: los rangos deben ser contiguos")
    for r in rangos:
        if r.desde is not None and r.hasta is not None and r.desde >= r.hasta:
            raise ValueError(f"{nombre}: rango con desde >= hasta")
    factores = [r.factor for r in rangos]
    if sentido == "directo" and factores != sorted(factores):
        raise ValueError(f"{nombre}: sentido 'directo' exige factores no decrecientes")
    if sentido == "inverso" and factores != sorted(factores, reverse=True):
        raise ValueError(f"{nombre}: sentido 'inverso' exige factores no crecientes")


class Senal(_Modelo):
    """Factor por una señal del perfil. `directo` = más valor, más riesgo; `inverso` = más valor, menos riesgo."""

    sentido: Literal["directo", "inverso"]
    rangos: list[Rango]

    @model_validator(mode="after")
    def _tabla_valida(self) -> Senal:
        _validar_tabla(self.rangos, self.sentido, "señal")
        return self


class SinDato(_Modelo):
    etapa: str
    factor: Dec = Field(gt=0)


class EtapaDeVida(_Modelo):
    campo: str
    sin_dato: SinDato
    rangos: list[RangoEtapa]

    @model_validator(mode="after")
    def _tabla_valida(self) -> EtapaDeVida:
        # Más trayectoria, más riesgo: la edad real no baja con los años.
        _validar_tabla(self.rangos, "directo", "etapa de vida")
        return self


class LimitesSuma(_Modelo):
    minimo: Dec = Field(gt=0)
    maximo: Dec = Field(gt=0)
    meses_de_ingreso: Dec = Field(gt=0)
    por_defecto: Dec = Field(gt=0)

    @model_validator(mode="after")
    def _coherentes(self) -> LimitesSuma:
        if self.minimo > self.maximo:
            raise ValueError("suma_asegurada: el mínimo supera al máximo")
        if not self.minimo <= self.por_defecto <= self.maximo:
            raise ValueError("suma_asegurada: el valor por defecto está fuera de los límites")
        return self


class BaseTarifa(_Modelo):
    tasa_anual: Dec = Field(gt=0)
    prima_minima: Dec = Field(ge=0)


class Cobertura(_Modelo):
    tipo: str
    porcentaje_valor: Dec = Field(gt=0)
    deducible_porcentaje: Dec = Field(default=Decimal(0), ge=0, le=1)


class ReglasPrecio(_Modelo):
    version: str = Field(min_length=1)
    producto: str
    nombre: str
    moneda: Literal["COP"]
    suma_asegurada: LimitesSuma
    base: BaseTarifa
    etapa_de_vida: EtapaDeVida
    senales: dict[str, Senal]
    recargo_degradado: Dec = Field(ge=0)
    coberturas: list[Cobertura]


def cargar_reglas(archivo: str | Path = ARCHIVO_POR_DEFECTO) -> ReglasPrecio:
    ruta = Path(archivo)
    if not ruta.is_absolute():
        ruta = DIRECTORIO_REGLAS / ruta
    try:
        crudo = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        return ReglasPrecio.model_validate(crudo)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ErrorReglas(f"No se pudieron cargar las reglas de precio {ruta.name}: {error}") from error
