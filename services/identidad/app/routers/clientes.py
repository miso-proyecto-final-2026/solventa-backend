import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.idempotency import guardar, obtener
from app.models import Cliente
from app.schemas import ClienteCreate, ClienteOut
from app.security import hash_password

router = APIRouter(prefix="/v1", tags=["clientes"])

MENSAJE_DUPLICADO = "No fue posible completar el registro con los datos proporcionados"


@router.post("/clientes", response_model=ClienteOut, status_code=201)
def crear_cliente(
    payload: ClienteCreate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ClienteOut:
    if idempotency_key:
        cacheado = obtener(idempotency_key)
        if cacheado is not None:
            status_code, cuerpo = cacheado
            if status_code >= 400:
                raise HTTPException(status_code=status_code, detail=cuerpo["detail"])
            return ClienteOut.model_validate(cuerpo)

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
        if idempotency_key:
            guardar(idempotency_key, 409, {"detail": MENSAJE_DUPLICADO})
        raise HTTPException(status_code=409, detail=MENSAJE_DUPLICADO)

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
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            guardar(idempotency_key, 409, {"detail": MENSAJE_DUPLICADO})
        raise HTTPException(status_code=409, detail=MENSAJE_DUPLICADO) from None
    db.refresh(cliente)

    cuerpo = ClienteOut(
        id=cliente.id,
        nombre=cliente.nombre,
        apellido=cliente.apellido,
        email=cliente.email,
        estado_kyc=cliente.estado_kyc,
        access_token=secrets.token_urlsafe(32),
        creado_en=cliente.creado_en,
    )
    if idempotency_key:
        guardar(idempotency_key, 201, cuerpo.model_dump(mode="json"))
    return cuerpo
