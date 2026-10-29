"""Reglas de disparo de las pólizas paramétricas y su evaluación (HU28, criterios 2, 3 y 8).

Las reglas viven en un archivo YAML versionado (`reglas/parametricas.yaml`), no en el código. Evaluar es una
función pura: dado el evento y la póliza, devuelve el resultado, el motivo y los valores que se miraron, que es
lo que se guarda para reconstruir la decisión (criterio 7).
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.siniestros.dominio import TIPOS_EVENTO

RUTA_REGLAS = Path(__file__).parent / "reglas" / "parametricas.yaml"

CORRESPONDE_PAGO = "CORRESPONDE_PAGO"
NO_CORRESPONDE = "NO_CORRESPONDE"

OPERADORES: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}


class ReglasInvalidas(ValueError):
    """El archivo de reglas no cumple la estructura esperada: el servicio no debe arrancar con él."""


@dataclass(frozen=True)
class Condicion:
    campo: str
    operador: str
    umbral: float


@dataclass(frozen=True)
class Regla:
    id: str
    producto: str
    tipo_evento: str
    exige_zona: bool
    monto_indemnizacion: int
    condiciones: tuple[Condicion, ...]


@dataclass(frozen=True)
class Reglas:
    version: str
    por_producto: dict[str, tuple[Regla, ...]] = field(default_factory=dict)

    @property
    def productos(self) -> frozenset[str]:
        return frozenset(self.por_producto)

    def para_evento(self, tipo_evento: str) -> list[Regla]:
        """Las reglas de todos los productos que reaccionan a este tipo de evento."""
        return [r for rs in self.por_producto.values() for r in rs if r.tipo_evento == tipo_evento]

    def regla(self, producto: str, tipo_evento: str) -> Regla | None:
        return next((r for r in self.por_producto.get(producto, ()) if r.tipo_evento == tipo_evento), None)


@dataclass(frozen=True)
class Decision:
    resultado: str
    motivo: str
    valores: dict[str, Any]
    monto_indemnizacion: int | None = None

    @property
    def corresponde_pago(self) -> bool:
        return self.resultado == CORRESPONDE_PAGO


def _condicion(crudo: Any, donde: str) -> Condicion:
    if not isinstance(crudo, dict):
        raise ReglasInvalidas(f"{donde}: la condición debe ser un objeto")
    campo, op, umbral = crudo.get("campo"), crudo.get("operador"), crudo.get("umbral")
    if not isinstance(campo, str) or not campo:
        raise ReglasInvalidas(f"{donde}: falta `campo`")
    if op not in OPERADORES:
        raise ReglasInvalidas(f"{donde}: operador `{op}` desconocido (use {', '.join(OPERADORES)})")
    if isinstance(umbral, bool) or not isinstance(umbral, (int, float)):
        raise ReglasInvalidas(f"{donde}: `umbral` debe ser numérico")
    return Condicion(campo, op, float(umbral))


def cargar_reglas(ruta: Path = RUTA_REGLAS) -> Reglas:
    """Lee y valida el archivo. Cualquier defecto lanza `ReglasInvalidas` (falla cerrado)."""
    try:
        crudo = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ReglasInvalidas(f"no se pudo leer {ruta}: {exc}") from exc
    if not isinstance(crudo, dict) or not isinstance(crudo.get("version"), str) or not crudo["version"]:
        raise ReglasInvalidas("falta `version`")
    productos = crudo.get("productos")
    if not isinstance(productos, dict) or not productos:
        raise ReglasInvalidas("falta `productos`")

    por_producto: dict[str, tuple[Regla, ...]] = {}
    for producto, definicion in productos.items():
        reglas: list[Regla] = []
        vistos: set[str] = set()
        for r in (definicion or {}).get("reglas") or []:
            donde = f"{producto}/{(r or {}).get('id', '?')}"
            if not isinstance(r, dict) or not isinstance(r.get("id"), str):
                raise ReglasInvalidas(f"{donde}: falta `id`")
            if r.get("tipo_evento") not in TIPOS_EVENTO:
                raise ReglasInvalidas(f"{donde}: `tipo_evento` debe ser uno de {sorted(TIPOS_EVENTO)}")
            if r["tipo_evento"] in vistos:
                raise ReglasInvalidas(f"{donde}: dos reglas para el mismo tipo de evento en un producto")
            vistos.add(r["tipo_evento"])
            monto = r.get("monto_indemnizacion")
            if isinstance(monto, bool) or not isinstance(monto, int) or monto <= 0:
                raise ReglasInvalidas(f"{donde}: `monto_indemnizacion` debe ser un entero positivo")
            reglas.append(
                Regla(
                    id=r["id"],
                    producto=producto,
                    tipo_evento=r["tipo_evento"],
                    exige_zona=bool(r.get("exige_zona", False)),
                    monto_indemnizacion=monto,
                    condiciones=tuple(_condicion(c, donde) for c in r.get("condiciones") or []),
                )
            )
        if not reglas:
            raise ReglasInvalidas(f"{producto}: no tiene reglas")
        por_producto[producto] = tuple(reglas)
    return Reglas(version=crudo["version"], por_producto=por_producto)


def _numero(valor: Any) -> float | None:
    return float(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else None


def evaluar(regla: Regla, datos: dict[str, Any], zona_evento: str | None, zona_poliza: str | None) -> Decision:
    """Aplica la regla de la póliza al evento. Los `valores` guardan lo que se comparó, también cuando no cumple."""
    valores: dict[str, Any] = {"regla": regla.id}
    if regla.exige_zona:
        valores["zona_evento"], valores["zona_poliza"] = zona_evento, zona_poliza
        if not zona_poliza:
            return Decision(NO_CORRESPONDE, "poliza_sin_zona_asegurada", valores)
        if zona_evento != zona_poliza:
            return Decision(NO_CORRESPONDE, "fuera_de_la_zona_asegurada", valores)

    for c in regla.condiciones:
        observado = _numero(datos.get(c.campo))
        valores[c.campo] = {"observado": datos.get(c.campo), "operador": c.operador, "umbral": c.umbral}
        if observado is None:
            return Decision(NO_CORRESPONDE, f"dato_faltante:{c.campo}", valores)
        if not OPERADORES[c.operador](observado, c.umbral):
            return Decision(NO_CORRESPONDE, f"condicion_no_cumplida:{c.campo}", valores)
    return Decision(CORRESPONDE_PAGO, "condicion_cumplida", valores, regla.monto_indemnizacion)
