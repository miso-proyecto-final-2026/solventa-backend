from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import idempotencia
from app.auth import crear_sesion, exigir_rol
from app.db import get_db
from app.models import Cliente
from app.routers.consentimientos import consentimiento_vigente
from app.schemas import (
    ClienteBusquedaOut,
    ClienteCreate,
    ClienteOut,
    ConsentimientoResumenOut,
    EstadoConsentimiento,
    TipoConsentimiento,
)
from app.security import hash_password
from app.tokens import ClaimsInternos
from app.validators import validar_documento, validar_tipo_documento

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


@router.get("/clientes/busqueda", response_model=ClienteBusquedaOut)
def buscar_cliente_por_documento(
    tipo_documento: str,
    numero_documento: str,
    db: Session = Depends(get_db),
    _asesor: ClaimsInternos = Depends(exigir_rol("ASESOR")),
) -> ClienteBusquedaOut:
    """Identifica a un cliente para la venta asistida (HU38). Solo asesores."""
    try:
        tipo = validar_tipo_documento(tipo_documento)
        numero = validar_documento(numero_documento)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    cliente = (
        db.query(Cliente)
        .filter(Cliente.tipo_documento == tipo, Cliente.numero_documento == numero)
        .first()
    )
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    c = consentimiento_vigente(db, cliente.id, TipoConsentimiento.OPEN_FINANCE_OPEN_DATA)
    consentimiento = (
        ConsentimientoResumenOut(estado=EstadoConsentimiento.NO_OTORGADO)
        if c is None
        else ConsentimientoResumenOut(
            estado=EstadoConsentimiento(c.estado),
            version_texto=c.version_texto,
            fecha_otorgamiento=c.fecha_otorgamiento,
        )
    )
    return ClienteBusquedaOut(
        cliente_id=cliente.id,
        nombre=cliente.nombre,
        apellido_inicial=(cliente.apellido[:1].upper() + ".") if cliente.apellido else "",
        tipo_documento=cliente.tipo_documento,
        numero_documento=cliente.numero_documento,
        estado_kyc=cliente.estado_kyc,
        consentimiento=consentimiento,
    )
