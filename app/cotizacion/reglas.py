"""Reglas de rating versionadas: modelo, validación y carga desde configuración (HU12).

Las tarifas, factores y recargos viven en `reglas/<version>.yaml`, nunca en el
código del motor. Se validan al cargar para que una regla mal escrita falle al
arrancar y no al cotizar.
"""

from __future__ import annotations

import itertools
import re
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

from app.cotizacion.errores import ErrorReglas

DIRECTORIO_REGLAS = Path(__file__).parent / "reglas"
_NOMBRE_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")


def _a_decimal(valor: object) -> object:
    # Los YAML traen floats (0.0035); se pasan por su repr para no arrastrar ruido binario.
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
        return (self.desde is None or valor >= self.desde) and (
            self.hasta is None or valor < self.hasta
        )


def _validar_tabla(rangos: list[Rango], sentido: str | None, nombre: str) -> None:
    if not rangos:
        raise ValueError(f"{nombre}: la tabla no puede estar vacía")
    if rangos[0].desde is not None or rangos[-1].hasta is not None:
        raise ValueError(
            f"{nombre}: el primer rango debe tener desde=null y el último hasta=null "
            "para cubrir todos los valores"
        )
    for anterior, actual in itertools.pairwise(rangos):
        if (
            anterior.hasta is None
            or actual.desde is None
            or anterior.hasta != actual.desde
        ):
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
    """Factor por una señal del perfil. `sentido` indica cómo crece el riesgo:
    `directo` = más valor, más riesgo; `inverso` = más valor, menos riesgo."""

    sentido: Literal["directo", "inverso"]
    rangos: list[Rango]

    @model_validator(mode="after")
    def _tabla_valida(self) -> Senal:
        _validar_tabla(self.rangos, self.sentido, "señal")
        return self


class FactorDato(_Modelo):
    """Factor por un campo de los datos del seguro: por rangos o por categorías."""

    rangos: list[Rango] | None = None
    categorias: dict[str, Dec] | None = None

    @model_validator(mode="after")
    def _uno_solo(self) -> FactorDato:
        if (self.rangos is None) == (self.categorias is None):
            raise ValueError("defina exactamente uno de 'rangos' o 'categorias'")
        if self.rangos is not None:
            _validar_tabla(self.rangos, None, "factor de dato")
        elif any(f <= 0 for f in (self.categorias or {}).values()):
            raise ValueError("los factores por categoría deben ser > 0")
        return self


class Campo(_Modelo):
    tipo: Literal["entero", "texto"]
    requerido: bool = True
    min: int | None = None
    max: int | None = None
    valores: list[str] | None = None


class BaseTarifa(_Modelo):
    campo_valor: str
    tasa_anual: Dec = Field(gt=0)
    prima_minima: Dec = Field(ge=0)


class ReglaCobertura(_Modelo):
    tipo: str
    porcentaje_valor: Dec = Field(gt=0)
    deducible_porcentaje: Dec = Field(default=Decimal(0), ge=0, le=1)


class ReglaProducto(_Modelo):
    nombre: str
    campos: dict[str, Campo]
    base: BaseTarifa
    factores_datos: dict[str, FactorDato] = Field(default_factory=dict)
    factor_riesgo_base: Dec = Field(gt=0)
    senales: dict[str, Senal] = Field(default_factory=dict)
    recargo_degradado: Dec = Field(ge=0)
    coberturas: list[ReglaCobertura] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherente(self) -> ReglaProducto:
        valor = self.campos.get(self.base.campo_valor)
        if valor is None or valor.tipo != "entero" or not valor.requerido:
            raise ValueError("base.campo_valor debe ser un campo entero y requerido")
        for nombre, factor in self.factores_datos.items():
            campo = self.campos.get(nombre)
            if campo is None:
                raise ValueError(f"factores_datos usa el campo desconocido '{nombre}'")
            if factor.rangos is not None and campo.tipo != "entero":
                raise ValueError(
                    f"'{nombre}': los rangos aplican solo a campos enteros"
                )
            if factor.categorias is not None and set(factor.categorias) != set(
                campo.valores or []
            ):
                raise ValueError(
                    f"'{nombre}': las categorías deben coincidir con 'valores'"
                )
        return self


class ReglasTarifarias(_Modelo):
    version: str
    moneda: Literal["COP"]
    vigencia_dias: int = Field(gt=0)
    productos: dict[str, ReglaProducto]


def cargar_reglas(version: str, directorio: Path | None = None) -> ReglasTarifarias:
    """Carga `<directorio>/<version>.yaml`. Lanza ErrorReglas si falta o es inválido."""
    if not _NOMBRE_VERSION.match(version):
        raise ErrorReglas(f"nombre de versión de reglas inválido: {version!r}")
    ruta = (directorio or DIRECTORIO_REGLAS) / f"{version}.yaml"
    if not ruta.is_file():
        raise ErrorReglas(f"no existe el archivo de reglas {ruta.name}")
    try:
        crudo = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        return ReglasTarifarias.model_validate(crudo)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ErrorReglas(f"reglas inválidas en {ruta.name}: {exc}") from exc
