"""Formato de montos y fechas en es-CO para el documento de la póliza (sin depender de la configuración regional)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
# Colombia no tiene horario de verano: un desfase fijo evita depender de la base de zonas horarias del sistema.
BOGOTA = timezone(timedelta(hours=-5))


def monto(valor: float) -> str:
    """COP sin decimales y con punto de miles: `$ 1.234.567`."""
    return f"$ {round(valor):,}".replace(",", ".")


def fecha(valor: date | datetime) -> str:
    """`26 de octubre de 2026`, en hora de Colombia si trae zona horaria."""
    if isinstance(valor, datetime):
        valor = (valor.astimezone(BOGOTA) if valor.tzinfo else valor).date()
    return f"{valor.day} de {MESES[valor.month - 1]} de {valor.year}"
