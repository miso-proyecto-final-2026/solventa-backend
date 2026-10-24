"""Registro de eventos procesados: la clave de idempotencia se valida contra la base antes de actuar (ADR-0002)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def tabla_eventos_procesados(metadata: MetaData, schema: str | None = None) -> Table:
    """Tabla `eventos_procesados` del servicio (en su propio esquema). Su migración de Alembic se copia del README."""
    return Table(
        "eventos_procesados",
        metadata,
        Column("consumidor", String(100), primary_key=True),
        Column("event_id", String(64), primary_key=True),
        Column("procesado_en", DateTime(timezone=True), nullable=False),
        Column("reentregas", Integer, nullable=False, default=0),
        schema=schema,
    )


class RegistroEventos:
    """Decide si un evento ya se procesó. La reserva vive en la transacción del manejador: si este falla, se deshace."""

    def __init__(self, tabla: Table) -> None:
        self._tabla = tabla

    def reservar(self, sesion: Session, consumidor: str, event_id: str) -> bool:
        """True la primera vez (la reserva queda en la transacción abierta de `sesion`); False si ya se había procesado.

        Una reentrega se cuenta en `reentregas`. Con dos réplicas procesando el mismo evento a la vez, la llave primaria hace
        que la segunda espere a la primera: si esta confirma, la segunda ve la reentrega; si falla, la segunda lo procesa.
        """
        try:
            with sesion.begin_nested():
                sesion.execute(
                    insert(self._tabla).values(consumidor=consumidor, event_id=event_id, procesado_en=datetime.now(UTC), reentregas=0)
                )
            return True
        except IntegrityError:
            sesion.execute(
                update(self._tabla)
                .where(self._tabla.c.consumidor == consumidor, self._tabla.c.event_id == event_id)
                .values(reentregas=self._tabla.c.reentregas + 1)
            )
            return False
