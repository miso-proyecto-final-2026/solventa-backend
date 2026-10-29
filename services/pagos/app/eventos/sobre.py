"""Sobre estándar de los eventos (documentación técnica §4.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

VERSION_SOBRE = "1.0"


class EventoInvalido(ValueError):
    """Los bytes recibidos no son un sobre de evento válido."""


def _ahora() -> datetime:
    return datetime.now(UTC)


class Evento(BaseModel):
    """Un evento de dominio. `payload` lleva los datos del negocio; el resto es el sobre.

    Los campos desconocidos se ignoran, para que un productor más nuevo no rompa a un consumidor más viejo.
    """

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    source: str = Field(min_length=1, max_length=100)
    timestamp: datetime = Field(default_factory=_ahora)
    correlation_id: str = Field(min_length=1, max_length=100)
    trace_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    version: str = VERSION_SOBRE
    payload: dict[str, Any]

    def a_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def desde_bytes(cls, datos: bytes) -> Evento:
        try:
            return cls.model_validate_json(datos)
        except ValidationError as error:
            raise EventoInvalido(str(error)) from error
