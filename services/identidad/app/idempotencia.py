"""Idempotencia de POST /v1/clientes persistida en la base de datos (HU01, criterio 6).

Con varias réplicas el estado en memoria no sirve: el reintento puede caer en un pod que no conoce la
clave. La base de datos es compartida, y su llave primaria resuelve la carrera entre pods.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import IdempotenciaCliente

TTL = timedelta(hours=24)


def _ahora() -> datetime:
    return datetime.now(UTC)


def huella(clave: str) -> str:
    """La clave nunca se guarda en claro."""
    return hashlib.sha256(clave.encode()).hexdigest()


def buscar(db: Session, clave: str) -> IdempotenciaCliente | None:
    """Devuelve el resultado vigente de la clave; si venció, lo elimina y no lo devuelve."""
    fila = db.get(IdempotenciaCliente, huella(clave))
    if fila is None:
        return None
    vencimiento = fila.expira_en if fila.expira_en.tzinfo else fila.expira_en.replace(tzinfo=UTC)
    if vencimiento <= _ahora():
        db.delete(fila)
        db.flush()
        return None
    return fila


def registrar(db: Session, clave: str, status_code: int, cliente_id: str | None = None) -> None:
    """Agrega el resultado a la transacción en curso (el llamador confirma) y purga lo vencido."""
    ahora = _ahora()
    db.query(IdempotenciaCliente).filter(IdempotenciaCliente.expira_en <= ahora).delete()
    db.add(
        IdempotenciaCliente(
            clave_hash=huella(clave),
            status_code=status_code,
            cliente_id=cliente_id,
            creado_en=ahora,
            expira_en=ahora + TTL,
        )
    )
