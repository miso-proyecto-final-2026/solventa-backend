"""Dominio de la verificación de identidad (HU02, KYC) de MS Identidad.

El puerto `VerificadorIdentidad` es estable (HA06): cada proveedor se integra con un adaptador y
el mapeo a su contrato vive solo allí.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

# Reto de prueba de vida que solicita el proveedor simulado (decisión de HU02).
RETO_VIDA = "GIRAR_IZQUIERDA"

# Motivo genérico: no revela qué comprobación falló (documento o prueba de vida).
MOTIVO_NO_SUPERADA = "VERIFICACION_NO_SUPERADA"
MOTIVO_PROVEEDOR_NO_DISPONIBLE = "PROVEEDOR_NO_DISPONIBLE"


class EstadoKyc(str, Enum):
    PENDIENTE = "PENDIENTE"
    VERIFICADO = "VERIFICADO"
    RECHAZADO = "RECHAZADO"
    EN_REVISION = "EN_REVISION"


class DecisionProveedor(str, Enum):
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


@dataclass(frozen=True)
class ImagenesKyc:
    """Imágenes capturadas. `repr=False`: nunca deben acabar en un log por accidente."""

    anverso: bytes = field(repr=False)
    reverso: bytes = field(repr=False)
    selfie: bytes = field(repr=False)


@dataclass(frozen=True)
class SolicitudVerificacion:
    cliente_id: str
    clave_idempotencia: str  # el proveedor no debe abrir dos casos con la misma clave
    reto: str
    imagenes: ImagenesKyc


@dataclass(frozen=True)
class ResultadoVerificacion:
    decision: DecisionProveedor
    referencia: str  # id del caso en el proveedor


class ProveedorKycError(Exception):
    """El proveedor falló (5xx, red, respuesta inválida). Se degrada a EN_REVISION."""


class VerificadorIdentidad(Protocol):
    async def verificar(self, solicitud: SolicitudVerificacion) -> ResultadoVerificacion: ...


class AlmacenEvidencias(Protocol):
    """Almacén cifrado y de acceso restringido de las imágenes KYC."""

    def guardar(self, clave: str, contenido: bytes, content_type: str) -> None: ...


class ErrorVerificacion(Exception):
    """Base de los errores de negocio de la verificación."""


class RetoInvalido(ErrorVerificacion):
    pass


class YaVerificado(ErrorVerificacion):
    pass


class EnRevision(ErrorVerificacion):
    pass


class IntentosAgotados(ErrorVerificacion):
    def __init__(self, hasta: datetime) -> None:
        super().__init__("Se alcanzó el máximo de intentos")
        self.hasta = hasta
