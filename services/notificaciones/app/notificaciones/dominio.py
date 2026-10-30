"""Dominio de MS Notificaciones (HU22): canales, estados y los puertos de envío (ADR-0004).

Los casos de uso solo conocen `CanalCorreo` y `CanalPush`. Cambiar el correo simulado por Amazon SES, o el push
simulado por SNS y FCM, es escribir otro adaptador y elegirlo en `wiring.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

IDIOMAS = ("es-CO", "en-US")
IDIOMA_POR_DEFECTO = "es-CO"


class Canal(StrEnum):
    PUSH = "PUSH"
    CORREO = "CORREO"


class EstadoEnvio(StrEnum):
    PENDIENTE = "PENDIENTE"
    ENVIADO = "ENVIADO"
    FALLIDO = "FALLIDO"


class TipoNotificacion(StrEnum):
    POLIZA_EMITIDA = "POLIZA_EMITIDA"


class Prioridad(StrEnum):
    ALTA = "ALTA"
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class Correo:
    destinatario: str
    asunto: str
    texto: str
    html: str


@dataclass(frozen=True)
class Push:
    """Lo que llega al teléfono. `texto_publico` es lo que se ve con la pantalla de bloqueo: sin datos de la póliza."""

    envio_id: str
    cliente_id: str
    titulo: str
    texto: str
    titulo_publico: str
    texto_publico: str
    prioridad: Prioridad
    tipo: TipoNotificacion
    suscripcion_id: str
    idioma: str


class ErrorEnvio(Exception):
    """El canal no pudo entregar. Solo se conserva el nombre de la clase en la base y en los logs: el mensaje puede traer el correo."""


class CanalCorreo(Protocol):
    async def enviar(self, correo: Correo) -> str:
        """Envía el correo y devuelve el identificador del mensaje en el proveedor."""
        ...


class CanalPush(Protocol):
    async def entregar(self, push: Push) -> str:
        """Entrega el push al cliente y devuelve un identificador de la entrega."""
        ...
