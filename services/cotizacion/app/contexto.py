"""Contexto de la solicitud en curso, compartido entre contextos de negocio (HU11).

Variables de contexto: viajan con la tarea asyncio y con `asyncio.to_thread`, así
el módulo de proveedores puede registrar la correlación y la cotización sin que
cada firma las reciba como parámetro.
"""

from __future__ import annotations

from contextvars import ContextVar

correlacion_id: ContextVar[str | None] = ContextVar("correlacion_id", default=None)
cotizacion_en_curso: ContextVar[str | None] = ContextVar(
    "cotizacion_en_curso", default=None
)
