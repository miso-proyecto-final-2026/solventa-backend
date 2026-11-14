"""Límite de intentos fallidos de contraseña (HU05): 5 fallos seguidos bloquean 15 minutos.

Se usa en el inicio de sesión de clientes y en la confirmación de contraseña al activar el acceso biométrico (HU04).
El identificador se guarda con hash: el bloqueo es el mismo exista o no la cuenta, así que no sirve para averiguar qué
correos están registrados.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IntentoAcceso

MAX_FALLOS = 5
BLOQUEO = timedelta(minutes=15)


def ahora() -> datetime:
    """Reloj del límite; las pruebas lo reemplazan para no esperar 15 minutos."""
    return datetime.now(UTC)


def clave(ambito: str, identificador: str) -> str:
    return hashlib.sha256(f"{ambito}:{identificador}".encode()).hexdigest()


def _con_zona(valor: datetime) -> datetime:
    # SQLite devuelve fechas sin zona; PostgreSQL las devuelve con zona.
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


def segundos_bloqueado(db: Session, clave_: str) -> int:
    """Segundos que faltan para poder reintentar; 0 si no está bloqueada."""
    intento = db.get(IntentoAcceso, clave_)
    if intento is None or intento.bloqueado_hasta is None:
        return 0
    faltan = (_con_zona(intento.bloqueado_hasta) - ahora()).total_seconds()
    return max(0, int(faltan) + (1 if faltan % 1 else 0))


def registrar_fallo(db: Session, clave_: str) -> int:
    """Suma un fallo; si llega al máximo bloquea la credencial. Devuelve los segundos de bloqueo (0 si no se bloqueó)."""
    momento = ahora()
    intento = db.get(IntentoAcceso, clave_)
    if intento is None:
        intento = IntentoAcceso(clave=clave_, fallidos=0, actualizado_en=momento)
        db.add(intento)
    elif (intento.bloqueado_hasta is not None and _con_zona(intento.bloqueado_hasta) <= momento) or (
        intento.bloqueado_hasta is None and _con_zona(intento.actualizado_en) + BLOQUEO <= momento
    ):
        # El bloqueo ya venció, o los fallos son de hace más de 15 minutos: se empieza de cero.
        intento.fallidos = 0
        intento.bloqueado_hasta = None
    intento.fallidos += 1
    intento.actualizado_en = momento
    if intento.fallidos >= MAX_FALLOS:
        intento.bloqueado_hasta = momento + BLOQUEO
    try:
        db.commit()
    except IntegrityError:  # otra réplica creó la fila al mismo tiempo: se cuenta este fallo sobre la suya
        db.rollback()
        return registrar_fallo(db, clave_)
    return segundos_bloqueado(db, clave_)


def limpiar(db: Session, clave_: str) -> None:
    """Un acceso correcto reinicia la cuenta de fallos."""
    intento = db.get(IntentoAcceso, clave_)
    if intento is not None:
        db.delete(intento)
        db.commit()
