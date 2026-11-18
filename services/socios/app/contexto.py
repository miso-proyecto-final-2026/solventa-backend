"""Contexto de la solicitud o del evento en curso.

Variables de contexto: viajan con la tarea asyncio y con `asyncio.to_thread`, así la capa de persistencia y los logs
registran la correlación y la traza sin que cada firma las reciba como parámetro.
"""

from __future__ import annotations

from contextvars import ContextVar

correlacion_id: ContextVar[str | None] = ContextVar("correlacion_id", default=None)
traza_id: ContextVar[str | None] = ContextVar("traza_id", default=None)
