"""Dominio de MS Pagos: transacciones, intentos de cobro y el puerto de la pasarela (ADR-0004).

El puerto `PasarelaPago` recibe y devuelve solo tipos de este módulo: ningún dato con la forma de un proveedor lo cruza.
Cambiar de pasarela es escribir otro adaptador y elegirlo en `wiring.py` (RQ-M2, EC-M2.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

# Algo con forma de PAN: 13 a 19 dígitos, con o sin espacios o guiones entre ellos.
_PARECE_PAN = re.compile(r"(?:\d[ -]?){12,18}\d")


def parece_pan(texto: str) -> bool:
    """Solventa solo recibe tokens de la pasarela (SAQ-A): un texto con forma de número de tarjeta se rechaza.

    Es la regla amplia, pensada para texto libre (notas): acepta cualquier mezcla de dígitos con espacios o guiones.
    Un identificador con guiones, como un UUID, puede caer en ella por azar: para tokens e identificadores use
    `identificador_parece_pan`.
    """
    return bool(_PARECE_PAN.search(texto))


# Cómo se escribe de verdad un número de tarjeta: corrido, en grupos de cuatro (4-4-4-4, con un último grupo más corto
# en los de 13 a 19 dígitos) o en el formato de American Express (4-6-5). Los grupos se separan siempre con el mismo
# carácter y empiezan y terminan en un límite (no junto a una letra, como el segmento hexadecimal de un UUID).
_PAN_CORRIDO = re.compile(r"\d{13,19}")
_PAN_AGRUPADO = re.compile(r"(?<![0-9A-Za-z])\d{4}([ -])\d{4}\1\d{4}\1\d{1,7}(?![0-9A-Za-z])")
_PAN_AMEX = re.compile(r"(?<![0-9A-Za-z])\d{4}([ -])\d{6}\1\d{5}(?![0-9A-Za-z])")


def identificador_parece_pan(texto: str) -> bool:
    """Como `parece_pan` pero sin falsos positivos con tokens ni UUID (`tok_sim_<uuid>`).

    Los guiones de un UUID separan segmentos de 8, 4, 4, 4 y 12 caracteres hexadecimales: sumando los dígitos de
    segmentos vecinos, la regla amplia confundía con una tarjeta a cerca del 2 % de ellos. Aquí solo se rechazan las
    formas reales de escribir un número de tarjeta.
    """
    return bool(_PAN_CORRIDO.search(texto) or _PAN_AGRUPADO.search(texto) or _PAN_AMEX.search(texto))


class TipoTransaccion(StrEnum):
    COBRO_PRIMA = "COBRO_PRIMA"
    REEMBOLSO_PRIMA = "REEMBOLSO_PRIMA"  # compensación de una firma fallida o vencida (HU21)


class EstadoTransaccion(StrEnum):
    """`PENDIENTE` mientras no haya una respuesta cierta de la pasarela (incluye las que esperan conciliación)."""

    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


ESTADOS_FINALES = (EstadoTransaccion.APROBADA.value, EstadoTransaccion.RECHAZADA.value)


class EstadoCaso(StrEnum):
    """Cobro fallido en gestión (HU19). `VENCIDO`: la gracia terminó sin recuperarlo y ya se avisó al operador."""

    PENDIENTE = "PENDIENTE"
    VENCIDO = "VENCIDO"
    RECUPERADO = "RECUPERADO"


ESTADOS_CASO_ABIERTOS = (EstadoCaso.PENDIENTE.value, EstadoCaso.VENCIDO.value)
# `origen` de las transacciones que crea un reintento desde la consola web (las otras son `kafka` y `rest`).
ORIGEN_GESTION = "gestion"


class ResultadoIntento(StrEnum):
    EN_CURSO = "EN_CURSO"              # se va a llamar a la pasarela (si queda así, el proceso cayó a mitad)
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    SIN_RESPUESTA = "SIN_RESPUESTA"    # no se sabe si cobró: se concilia antes de volver a cobrar
    NO_DISPONIBLE = "NO_DISPONIBLE"    # la pasarela no recibió la orden: se reintenta
    CONCILIADO = "CONCILIADO"          # el resultado salió de consultar la pasarela, no de cobrar


@dataclass(frozen=True)
class SolicitudCobro:
    idempotency_key: str
    token: str
    monto: Decimal
    moneda: str
    referencia: str    # id de la suscripción: la pasarela solo conoce una referencia opaca


@dataclass(frozen=True)
class ResultadoCobro:
    aprobado: bool
    referencia_pasarela: str | None = None
    codigo: str | None = None
    motivo: str | None = None


@dataclass(frozen=True)
class DatosToken:
    ultimos4: str
    marca: str


class PasarelaSinRespuesta(Exception):
    """La orden pudo llegar a la pasarela, pero no hay respuesta (tiempo agotado o respuesta perdida)."""


class PasarelaNoDisponible(Exception):
    """La pasarela no recibió la orden (conexión rechazada): reintentar no duplica."""


class TokenDesconocido(Exception):
    """La pasarela no reconoce el token."""


class PasarelaPago(Protocol):
    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        """Cobra una sola vez por `idempotency_key`: repetirla devuelve el resultado del primer cobro."""
        ...

    async def consultar(self, idempotency_key: str) -> ResultadoCobro | None:
        """El resultado de un cobro ya hecho con esa clave, o None si la pasarela no lo tiene."""
        ...

    async def reembolsar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        """Devuelve un cobro una sola vez por `idempotency_key` (la del reembolso, distinta a la del cobro)."""
        ...

    async def describir_token(self, token: str) -> DatosToken:
        """Marca y últimos 4 dígitos de la tarjeta detrás del token (nunca el número completo)."""
        ...
