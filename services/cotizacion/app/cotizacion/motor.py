"""Motor de rating: función pura sobre (producto, datos, perfil, reglas) (HU12).

No lee reloj, base de datos, caché ni red: la misma entrada con la misma versión
de reglas produce exactamente la misma prima (criterio 5). Agregar un producto
es agregar un bloque a las reglas; este módulo no cambia.

Redondeo: COP sin decimales, ROUND_HALF_UP, aplicado en cada paso. Los
componentes se obtienen por diferencia entre pasos, así que su suma es
exactamente la prima anual.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.cotizacion.dominio import (
    AJUSTE_DATOS,
    AJUSTE_RIESGO,
    ETAPA_DATO,
    ETAPA_RIESGO_BASE,
    ETAPA_SENAL,
    MONEDA,
    PRIMA_BASE,
    RECARGO_DEGRADADO,
    VERSION_DESGLOSE,
    CoberturaCalculada,
    Componente,
    DesglosePrecio,
    FactorAplicado,
    PerfilUnificado,
    PerfilUsado,
    ResultadoRating,
)
from app.cotizacion.errores import DetalleError, ErrorValidacion
from app.cotizacion.reglas import (
    Campo,
    FactorDato,
    ReglaProducto,
    ReglasTarifarias,
    Senal,
)

_UNO = Decimal(1)
_MESES = Decimal(12)


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(_UNO, rounding=ROUND_HALF_UP)


def _error(campo: str, motivo: str) -> DetalleError:
    return DetalleError(campo=campo, motivo=motivo)


def _validar_campo(
    nombre: str, spec: Campo, datos: Mapping[str, Any]
) -> list[DetalleError]:
    ruta = f"datos.{nombre}"
    if nombre not in datos or datos[nombre] is None:
        return [_error(ruta, "es requerido")] if spec.requerido else []
    valor = datos[nombre]
    if spec.tipo == "entero":
        if isinstance(valor, bool) or not isinstance(valor, int):
            return [_error(ruta, "debe ser un número entero")]
        if spec.min is not None and valor < spec.min:
            return [_error(ruta, f"debe ser mayor o igual a {spec.min}")]
        if spec.max is not None and valor > spec.max:
            return [_error(ruta, f"debe ser menor o igual a {spec.max}")]
    elif not isinstance(valor, str) or not valor:
        return [_error(ruta, "debe ser un texto")]
    elif spec.valores is not None and valor not in spec.valores:
        return [
            _error(ruta, f"valor no permitido; use uno de: {', '.join(spec.valores)}")
        ]
    return []


def validar_solicitud(
    producto: str, datos: Mapping[str, Any], reglas: ReglasTarifarias
) -> ReglaProducto:
    """Valida producto y datos contra las reglas. Lanza ErrorValidacion sin calcular."""
    regla = reglas.productos.get(producto)
    if regla is None:
        disponibles = ", ".join(sorted(reglas.productos))
        raise ErrorValidacion(
            [_error("producto", f"producto no existe; disponibles: {disponibles}")]
        )
    detalles: list[DetalleError] = []
    for nombre, spec in regla.campos.items():
        detalles.extend(_validar_campo(nombre, spec, datos))
    detalles.extend(
        _error(f"datos.{extra}", "campo no reconocido para este producto")
        for extra in sorted(set(datos) - set(regla.campos))
    )
    if detalles:
        raise ErrorValidacion(detalles)
    return regla


def _factor_dato(factor: FactorDato, valor: Any) -> Decimal:
    if factor.categorias is not None:
        return factor.categorias[valor]
    return _factor_en_rangos(factor.rangos or [], Decimal(valor))


def _factor_en_rangos(rangos: list[Any], valor: Decimal) -> Decimal:
    for rango in rangos:
        if rango.contiene(valor):
            return rango.factor
    raise AssertionError("tabla sin cobertura total; el cargador de reglas lo impide")


def _factores_senales(
    senales: Mapping[str, Senal], perfil: PerfilUnificado
) -> list[FactorAplicado]:
    aplicados = []
    for nombre, tabla in senales.items():
        valor = perfil.senales.get(nombre)
        if valor is None or isinstance(valor, bool):
            continue  # señal ausente: no ajusta
        fuente, obtenido_en = perfil.procedencia.get(nombre, (None, None))
        aplicados.append(
            FactorAplicado(
                ETAPA_SENAL,
                nombre,
                _factor_en_rangos(tabla.rangos, Decimal(str(valor))),
                fuente,
                obtenido_en,
            )
        )
    return aplicados


def calcular_prima(
    producto: str,
    datos: Mapping[str, Any],
    perfil: PerfilUnificado,
    reglas: ReglasTarifarias,
) -> ResultadoRating:
    regla = validar_solicitud(producto, datos, reglas)
    valor_asegurado = Decimal(datos[regla.base.campo_valor])

    base = max(
        _redondear(valor_asegurado * regla.base.tasa_anual), regla.base.prima_minima
    )

    factores: list[FactorAplicado] = []
    factor_datos = _UNO
    for nombre, factor in regla.factores_datos.items():
        aplicado = _factor_dato(factor, datos[nombre])
        factor_datos *= aplicado
        factores.append(FactorAplicado(ETAPA_DATO, nombre, aplicado))
    subtotal_datos = _redondear(base * factor_datos)

    # Solo un perfil con consentimiento y sin degradar aporta señales; en los demás
    # casos rige el factor base del producto.
    usa_senales = perfil.con_consentimiento and not perfil.degradado
    factor_riesgo = regla.factor_riesgo_base
    factores.append(
        FactorAplicado(ETAPA_RIESGO_BASE, "factor_riesgo_base", factor_riesgo)
    )
    if usa_senales:
        factores_senales = _factores_senales(regla.senales, perfil)
        factores.extend(factores_senales)
        factor_senales = _UNO
        for aplicado in factores_senales:
            factor_senales *= aplicado.factor
        factor_riesgo *= factor_senales
    subtotal_riesgo = _redondear(subtotal_datos * factor_riesgo)

    recargo = (
        _redondear(subtotal_riesgo * regla.recargo_degradado)
        if perfil.degradado
        else Decimal(0)
    )
    prima_anual = subtotal_riesgo + recargo
    componentes = (
        Componente(PRIMA_BASE, base),
        Componente(AJUSTE_DATOS, subtotal_datos - base),
        Componente(AJUSTE_RIESGO, subtotal_riesgo - subtotal_datos),
        Componente(RECARGO_DEGRADADO, recargo),
    )

    coberturas = []
    for cob in regla.coberturas:
        monto = _redondear(valor_asegurado * cob.porcentaje_valor)
        coberturas.append(
            CoberturaCalculada(
                tipo=cob.tipo,
                monto=monto,
                deducible=_redondear(monto * cob.deducible_porcentaje),
            )
        )

    return ResultadoRating(
        producto=producto,
        moneda=MONEDA,
        prima_anual=prima_anual,
        prima_mensual=_redondear(prima_anual / _MESES),
        coberturas=tuple(coberturas),
        componentes=componentes,
        version_reglas=reglas.version,
        vigencia_dias=reglas.vigencia_dias,
        degradado=perfil.degradado,
        aproximada=not perfil.con_consentimiento,
        desglose=DesglosePrecio(
            version_esquema=VERSION_DESGLOSE,
            version_reglas=reglas.version,
            producto=producto,
            moneda=MONEDA,
            prima_base=base,
            factores=tuple(factores),
            recargo_degradado_pct=(
                regla.recargo_degradado if perfil.degradado else None
            ),
            componentes=componentes,
            prima_anual=prima_anual,
            perfil=PerfilUsado(
                fuentes=perfil.fuentes,
                degradado=perfil.degradado,
                con_consentimiento=perfil.con_consentimiento,
                completitud=perfil.completitud,
            ),
        ),
    )
