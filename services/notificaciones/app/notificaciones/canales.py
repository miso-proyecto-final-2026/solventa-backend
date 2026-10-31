"""Adaptadores de envío (ADR-0004).

- `PushSimulado`: no llama a ningún proveedor. «Entregar» es dejar el mensaje en la bandeja del cliente
  (`bandeja_push`), que la app consulta y muestra como notificación del sistema. Se simula el transporte; la
  idempotencia, los reintentos, las plantillas y la bandeja son reales. Un push real con SNS y FCM sería otro adaptador.
- `CorreoSimulado`: no envía nada; deja constancia en el log, sin la dirección.
- `CorreoSes`: Amazon SES. Con la cuenta en sandbox solo se envía a direcciones verificadas.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.notificaciones.dominio import Correo, ErrorEnvio, Push
from app.notificaciones.modelos_db import BandejaPushDB

log = logging.getLogger("notificaciones.canales")


def _ahora() -> datetime:
    return datetime.now(UTC)


class PushSimulado:
    def __init__(self, fabrica: sessionmaker[Session], reloj: Callable[[], datetime] = _ahora) -> None:
        self._fabrica = fabrica
        self._reloj = reloj

    async def entregar(self, push: Push) -> str:
        await asyncio.to_thread(self._guardar, push)
        log.info("push entregado a la bandeja envio_id=%s prioridad=%s idioma=%s", push.envio_id, push.prioridad.value, push.idioma)
        return f"bandeja:{push.envio_id}"

    def _guardar(self, push: Push) -> None:
        """Idempotente: entregar dos veces el mismo envío (un reintento tras caer a mitad) deja una sola fila."""
        fila = BandejaPushDB(
            envio_id=push.envio_id,
            cliente_id=push.cliente_id,
            tipo=push.tipo.value,
            suscripcion_id=push.suscripcion_id,
            titulo=push.titulo,
            texto=push.texto,
            titulo_publico=push.titulo_publico,
            texto_publico=push.texto_publico,
            prioridad=push.prioridad.value,
            idioma=push.idioma,
            creado_en=self._reloj(),
        )
        try:
            with self._fabrica() as s, s.begin():
                s.add(fila)
        except IntegrityError:
            return


class CorreoSimulado:
    """Cuenta los correos «enviados» para poder comprobarlos; en el log solo queda una huella de la dirección."""

    def __init__(self) -> None:
        self.enviados = 0

    async def enviar(self, correo: Correo) -> str:
        self.enviados += 1
        referencia = "simulado-" + hashlib.sha256(f"{correo.destinatario}{correo.asunto}{self.enviados}".encode()).hexdigest()[:12]
        log.info("correo simulado enviado referencia=%s asunto=%s", referencia, correo.asunto)
        return referencia


class CorreoSes:
    """`SendEmail` de SES. Cualquier fallo del proveedor se reduce a `ErrorEnvio` con el nombre del error, sin su mensaje."""

    def __init__(self, remitente: str, region: str | None = None, cliente: Any = None) -> None:
        if not remitente:
            raise ValueError("CORREO_REMITENTE es obligatorio con CORREO_PROVEEDOR=ses.")
        if cliente is None:
            import boto3

            cliente = boto3.client("ses", region_name=region)
        self._remitente = remitente
        self._ses = cliente

    async def enviar(self, correo: Correo) -> str:
        try:
            respuesta = await asyncio.to_thread(
                self._ses.send_email,
                Source=self._remitente,
                Destination={"ToAddresses": [correo.destinatario]},
                Message={
                    "Subject": {"Data": correo.asunto, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": correo.texto, "Charset": "UTF-8"},
                        "Html": {"Data": correo.html, "Charset": "UTF-8"},
                    },
                },
            )
        except Exception as error:  # noqa: BLE001 - boto3 lanza muchas clases; todas se reducen a un fallo del canal
            codigo = getattr(error, "response", {}).get("Error", {}).get("Code") if hasattr(error, "response") else None
            raise ErrorEnvio(codigo or type(error).__name__) from None
        return str(respuesta["MessageId"])
