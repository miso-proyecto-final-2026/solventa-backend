from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import idempotencia
from app.auth import crear_sesion
from app.db import get_db
from app.models import Cliente
from app.schemas import ClienteCreate, ClienteOut
from app.security import hash_password

router = APIRouter(prefix="/v1", tags=["clientes"])

MENSAJE_DUPLICADO = "No fue posible completar el registro con los datos proporcionados"


def _cliente_out(cliente: Cliente, access_token: str) -> ClienteOut:
    return ClienteOut(
        id=cliente.id,
        nombre=cliente.nombre,
        apellido=cliente.apellido,
        email=cliente.email,
        estado_kyc=cliente.estado_kyc,
        access_token=access_token,
        creado_en=cliente.creado_en,
    )


def _repetir(db: Session, clave: str) -> ClienteOut | None:
    """Repite el resultado de un intento anterior con la misma clave, o devuelve None si no hay.

    El token de sesión no se guarda: un reintento exitoso recibe una sesión nueva sobre el mismo cliente.
    """
    previo = idempotencia.buscar(db, clave)
    if previo is None:
        return None
    if previo.status_code >= 400:
        raise HTTPException(status_code=previo.status_code, detail=MENSAJE_DUPLICADO)
    cliente = db.get(Cliente, previo.cliente_id) if previo.cliente_id else None
    if cliente is None:  # el cliente ya no existe: la clave no tiene a qué apuntar
        db.delete(previo)
        db.commit()
        return None
    access_token = crear_sesion(db, cliente.id)
    db.commit()
    return _cliente_out(cliente, access_token)


def _duplicado(db: Session, clave: str | None) -> HTTPException:
    """409 genérico; con clave, el resultado también queda registrado para repetirlo en un reintento."""
    if clave:
        try:
            idempotencia.registrar(db, clave, 409)
            db.commit()
        except IntegrityError:  # otra réplica ya lo registró
            db.rollback()
    return HTTPException(status_code=409, detail=MENSAJE_DUPLICADO)


@router.post("/clientes", response_model=ClienteOut, status_code=201)
def crear_cliente(
    payload: ClienteCreate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ClienteOut:
    if idempotency_key:
        repetido = _repetir(db, idempotency_key)
        if repetido is not None:
            return repetido

    existente = (
        db.query(Cliente)
        .filter(
            or_(
                Cliente.numero_documento == payload.numero_documento,
                Cliente.email == payload.email,
            )
        )
        .first()
    )
    if existente is not None:
        # Si otra réplica acaba de crear este cliente con la misma clave, es un reintento, no un duplicado.
        if idempotency_key and (repetido := _repetir(db, idempotency_key)) is not None:
            return repetido
        raise _duplicado(db, idempotency_key)

    cliente = Cliente(
        nombre=payload.nombre,
        apellido=payload.apellido,
        tipo_documento=payload.tipo_documento,
        numero_documento=payload.numero_documento,
        email=payload.email,
        celular=payload.celular,
        password_hash=hash_password(payload.password),
    )
    db.add(cliente)
    try:
        db.flush()
        access_token = crear_sesion(db, cliente.id)
        if idempotency_key:
            idempotencia.registrar(db, idempotency_key, 201, cliente.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        # La clave o el cliente ya existen: con la misma clave es un reintento concurrente de otra réplica.
        if idempotency_key and (repetido := _repetir(db, idempotency_key)) is not None:
            return repetido
        raise _duplicado(db, idempotency_key) from None
    db.refresh(cliente)
    return _cliente_out(cliente, access_token)
