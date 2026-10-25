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
    """Solventa solo recibe tokens de la pasarela (SAQ-A): un texto con forma de número de tarjeta se rechaza."""
    return bool(_PARECE_PAN.search(texto))


class TipoTransaccion(StrEnum):
    COBRO_PRIMA = "COBRO_PRIMA"


class EstadoTransaccion(StrEnum):
    """`PENDIENTE` mientras no haya una respuesta cierta de la pasarela (incluye las que esperan conciliación)."""

    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


ESTADOS_FINALES = (EstadoTransaccion.APROBADA.value, EstadoTransaccion.RECHAZADA.value)


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

    async def describir_token(self, token: str) -> DatosToken:
        """Marca y últimos 4 dígitos de la tarjeta detrás del token (nunca el número completo)."""
        ...
