"""Serialización, recálculo e invariante del desglose del precio (HU14).

Funciones puras. El desglose se guarda como JSON versionado (ver ADR-0006): los
montos y factores viajan como texto decimal para no perder exactitud, y las
fechas en ISO 8601. No contiene valores del cliente, solo factores y referencias.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.cotizacion.dominio import (
    ETAPA_DATO,
    ETAPA_RIESGO_BASE,
    ETAPA_SENAL,
    VERSION_DESGLOSE,
    Componente,
    DesglosePrecio,
    FactorAplicado,
    PerfilUsado,
)
from app.cotizacion.errores import ErrorDesglose

_UNO = Decimal(1)


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(_UNO, rounding=ROUND_HALF_UP)


def _producto(factores: list[Decimal]) -> Decimal:
    total = _UNO
    for f in factores:
        total *= f
    return total


def recalcular(desglose: DesglosePrecio) -> Decimal:
    """Recalcula la prima anual solo a partir del desglose (criterio 6)."""
    por_etapa: dict[str, list[Decimal]] = {
        ETAPA_DATO: [],
        ETAPA_RIESGO_BASE: [],
        ETAPA_SENAL: [],
    }
    for f in desglose.factores:
        por_etapa[f.etapa].append(f.factor)
    subtotal_datos = _redondear(desglose.prima_base * _producto(por_etapa[ETAPA_DATO]))
    factor_riesgo = _producto(por_etapa[ETAPA_RIESGO_BASE]) * _producto(
        por_etapa[ETAPA_SENAL]
    )
    subtotal_riesgo = _redondear(subtotal_datos * factor_riesgo)
    recargo = (
        _redondear(subtotal_riesgo * desglose.recargo_degradado_pct)
        if desglose.recargo_degradado_pct is not None
        else Decimal(0)
    )
    return subtotal_riesgo + recargo


def verificar_invariante(desglose: DesglosePrecio) -> None:
    """La suma de componentes debe ser la prima final (criterio 2).

    Además exige que el desglose permita reproducir esa prima: un desglose que no
    explica el precio no sirve para auditar. Lanza `ErrorDesglose` si falla.
    """
    suma = sum((c.monto for c in desglose.componentes), Decimal(0))
    if suma != desglose.prima_anual:
        raise ErrorDesglose(
            f"la suma de componentes ({suma}) no coincide con la prima ({desglose.prima_anual})"
        )
    recalculada = recalcular(desglose)
    if recalculada != desglose.prima_anual:
        raise ErrorDesglose(
            f"el desglose no reproduce la prima ({recalculada} != {desglose.prima_anual})"
        )


def a_json(d: DesglosePrecio) -> dict[str, Any]:
    return {
        "version_esquema": d.version_esquema,
        "version_reglas": d.version_reglas,
        "producto": d.producto,
        "moneda": d.moneda,
        "prima_base": str(d.prima_base),
        "factores": [
            {
                "etapa": f.etapa,
                "nombre": f.nombre,
                "factor": str(f.factor),
                "fuente": f.fuente,
                "obtenido_en": f.obtenido_en.isoformat() if f.obtenido_en else None,
            }
            for f in d.factores
        ],
        "recargo_degradado_pct": (
            None if d.recargo_degradado_pct is None else str(d.recargo_degradado_pct)
        ),
        "componentes": [
            {"concepto": c.concepto, "monto": str(c.monto)} for c in d.componentes
        ],
        "prima_anual": str(d.prima_anual),
        "perfil": {
            "fuentes": list(d.perfil.fuentes),
            "degradado": d.perfil.degradado,
            "con_consentimiento": d.perfil.con_consentimiento,
            "completitud": d.perfil.completitud,
        },
    }


def desde_json(datos: dict[str, Any]) -> DesglosePrecio:
    if datos.get("version_esquema") != VERSION_DESGLOSE:
        raise ErrorDesglose(
            f"versión de desglose no soportada: {datos.get('version_esquema')}"
        )
    pct = datos["recargo_degradado_pct"]
    perfil = datos["perfil"]
    return DesglosePrecio(
        version_esquema=datos["version_esquema"],
        version_reglas=datos["version_reglas"],
        producto=datos["producto"],
        moneda=datos["moneda"],
        prima_base=Decimal(datos["prima_base"]),
        factores=tuple(
            FactorAplicado(
                f["etapa"],
                f["nombre"],
                Decimal(f["factor"]),
                f["fuente"],
                datetime.fromisoformat(f["obtenido_en"]) if f["obtenido_en"] else None,
            )
            for f in datos["factores"]
        ),
        recargo_degradado_pct=None if pct is None else Decimal(pct),
        componentes=tuple(
            Componente(c["concepto"], Decimal(c["monto"])) for c in datos["componentes"]
        ),
        prima_anual=Decimal(datos["prima_anual"]),
        perfil=PerfilUsado(
            fuentes=tuple(perfil["fuentes"]),
            degradado=perfil["degradado"],
            con_consentimiento=perfil["con_consentimiento"],
            completitud=perfil["completitud"],
        ),
    )
