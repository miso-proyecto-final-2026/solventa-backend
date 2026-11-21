"""Cálculo del precio individualizado del seguro de vida hipotecario: función pura (HU25).

No lee reloj, base de datos, caché ni red: el mismo perfil con la misma versión de reglas produce exactamente la
misma prima. Redondeo COP sin decimales, ROUND_HALF_UP, aplicado en cada paso, como el motor de rating de
Cotización; el ajuste de cada paso es la diferencia con el anterior, así que la base más los ajustes suman
exactamente la prima anual.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.perfilamiento.reglas_precio import Rango, ReglasPrecio

_UNO = Decimal(1)
_MESES = Decimal(12)

PASO_BASE = "prima_base"
PASO_ETAPA = "etapa_de_vida"
PASO_SENAL = "senal"
PASO_DEGRADADO = "recargo_degradado"


class SumaFueraDeRango(ValueError):
    """La suma asegurada pedida no cabe en los límites de las reglas."""

    def __init__(self, minimo: Decimal, maximo: Decimal) -> None:
        super().__init__(f"debe estar entre {minimo:,.0f} y {maximo:,.0f}")
        self.minimo = minimo
        self.maximo = maximo


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(_UNO, rounding=ROUND_HALF_UP)


def _factor(rangos: list[Rango], valor: Decimal) -> Rango:
    return next(r for r in rangos if r.contiene(valor))


@dataclass(frozen=True)
class Paso:
    """Un paso del cálculo: qué se aplicó, con qué valor del perfil y cuánto movió la prima."""

    paso: str
    nombre: str
    factor: Decimal | None
    valor_perfil: Any | None
    ajuste: Decimal
    acumulado: Decimal
    sin_dato: bool = False

    def a_dict(self) -> dict[str, Any]:
        return {
            "paso": self.paso,
            "nombre": self.nombre,
            "factor": None if self.factor is None else str(self.factor),
            "valor_perfil": self.valor_perfil,
            "ajuste": int(self.ajuste),
            "acumulado": int(self.acumulado),
            "sin_dato": self.sin_dato,
        }


@dataclass(frozen=True)
class OfertaCalculada:
    version_reglas: str
    producto: str
    moneda: str
    suma_asegurada: Decimal
    etapa_de_vida: str
    prima_anual: Decimal
    prima_mensual: Decimal
    pasos: tuple[Paso, ...]
    coberturas: tuple[dict[str, Any], ...]
    degradado: bool

    def desglose(self) -> list[dict[str, Any]]:
        return [p.a_dict() for p in self.pasos]


def suma_por_defecto(campos: Mapping[str, Mapping[str, Any]], reglas: ReglasPrecio) -> Decimal:
    """Suma asegurada cuando la consulta no trae una: ingresos por `meses_de_ingreso`, dentro de los límites."""
    limites = reglas.suma_asegurada
    ingresos = campos.get("ingresos_mensuales_cop")
    if ingresos is None:
        return limites.por_defecto
    suma = _redondear(Decimal(str(ingresos["valor"])) * limites.meses_de_ingreso)
    return min(max(suma, limites.minimo), limites.maximo)


def calcular_oferta(
    campos: Mapping[str, Mapping[str, Any]],
    *,
    degradado: bool,
    suma_asegurada: Decimal | None,
    reglas: ReglasPrecio,
) -> OfertaCalculada:
    """`campos` es el `campos` del perfil canónico: {nombre: {"valor": ..., "fuente": ...}}."""
    limites = reglas.suma_asegurada
    suma = suma_por_defecto(campos, reglas) if suma_asegurada is None else Decimal(suma_asegurada)
    if not limites.minimo <= suma <= limites.maximo:
        raise SumaFueraDeRango(limites.minimo, limites.maximo)

    pasos: list[Paso] = []
    base = max(_redondear(reglas.base.tasa_anual * suma), reglas.base.prima_minima)
    pasos.append(Paso(PASO_BASE, f"{reglas.base.tasa_anual} sobre la suma asegurada", None, None, base, base))
    valor = base

    # Etapa de vida.
    espec = reglas.etapa_de_vida
    campo = campos.get(espec.campo)
    if campo is None:
        etapa, factor, sin_dato, visto = espec.sin_dato.etapa, espec.sin_dato.factor, True, None
    else:
        visto = campo["valor"]
        rango = _factor(espec.rangos, Decimal(str(visto)))
        etapa, factor, sin_dato = rango.etapa, rango.factor, False
    nuevo = _redondear(valor * factor)
    pasos.append(Paso(PASO_ETAPA, etapa, factor, visto, nuevo - valor, nuevo, sin_dato))
    valor = nuevo

    # Señales del perfil, en el orden del archivo de reglas.
    for nombre, senal in reglas.senales.items():
        campo = campos.get(nombre)
        if campo is None:
            pasos.append(Paso(PASO_SENAL, nombre, Decimal(1), None, Decimal(0), valor, sin_dato=True))
            continue
        visto = campo["valor"]
        factor = _factor(senal.rangos, Decimal(str(visto))).factor
        nuevo = _redondear(valor * factor)
        pasos.append(Paso(PASO_SENAL, nombre, factor, visto, nuevo - valor, nuevo))
        valor = nuevo

    if degradado:
        nuevo = valor + _redondear(valor * reglas.recargo_degradado)
        pasos.append(Paso(PASO_DEGRADADO, "datos de respaldo", reglas.recargo_degradado, None, nuevo - valor, nuevo))
        valor = nuevo

    return OfertaCalculada(
        version_reglas=reglas.version,
        producto=reglas.producto,
        moneda=reglas.moneda,
        suma_asegurada=suma,
        etapa_de_vida=pasos[1].nombre,
        prima_anual=valor,
        prima_mensual=_redondear(valor / _MESES),
        pasos=tuple(pasos),
        coberturas=tuple(
            {
                "tipo": c.tipo,
                "monto_cubierto": int(_redondear(suma * c.porcentaje_valor)),
                "deducible_porcentaje": str(c.deducible_porcentaje),
            }
            for c in reglas.coberturas
        ),
        degradado=degradado,
    )
