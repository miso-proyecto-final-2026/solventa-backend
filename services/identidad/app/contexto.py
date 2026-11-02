"""Contexto de la solicitud en curso.

Variable de contexto: viaja con la tarea asyncio y con `asyncio.to_thread`, así la capa de
persistencia registra la correlación sin que cada firma la reciba como parámetro.
"""

from __future__ import annotations

from contextvars import ContextVar

correlacion_id: ContextVar[str | None] = ContextVar("correlacion_id", default=None)
traza_id: ContextVar[str | None] = ContextVar("traza_id", default=None)
