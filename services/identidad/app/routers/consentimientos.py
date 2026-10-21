from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Acceso, acceso_actual, exigir_cliente
from app.db import get_db
from app.models import Cliente, Consentimiento, ConsentimientoRegistro
from app.schemas import (
    ConsentimientoCreate,
    ConsentimientoOut,
    EstadoConsentimiento,
    TipoConsentimiento,
)

router = APIRouter(prefix="/v1/clientes/{cliente_id}/consentimientos", tags=["consentimientos"])


def _salida(
    cliente_id: str, tipo: TipoConsentimiento, c: Consentimiento | None
) -> ConsentimientoOut:
    if c is None:
        return ConsentimientoOut(
            cliente_id=cliente_id, tipo=tipo, estado=EstadoConsentimiento.NO_OTORGADO
        )
    return ConsentimientoOut(
        id=c.id,
        cliente_id=cliente_id,
        tipo=tipo,
        estado=EstadoConsentimiento(c.estado),
        version_texto=c.version_texto,
        fecha_otorgamiento=c.fecha_otorgamiento,
        fecha_revocacion=c.fecha_revocacion,
    )


def consentimiento_vigente(db: Session, cliente_id: str, tipo: TipoConsentimiento) -> Consentimiento | None:
    return (
        db.query(Consentimiento)
        .filter(Consentimiento.cliente_id == cliente_id, Consentimiento.tipo == tipo.value)
        .first()
    )


@router.post("", response_model=ConsentimientoOut, status_code=201)
def otorgar_consentimiento(
    cliente_id: str,
    payload: ConsentimientoCreate,
    response: Response,
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
) -> ConsentimientoOut:
    exigir_cliente(cliente_id, acceso)
    if db.get(Cliente, cliente_id) is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    existente = consentimiento_vigente(db, cliente_id, payload.tipo)
    if existente is not None and existente.estado == EstadoConsentimiento.OTORGADO.value:
        # Idempotencia: reintentar no crea otro consentimiento vigente ni otro registro.
        response.status_code = 200
        return _salida(cliente_id, payload.tipo, existente)

    ahora = datetime.now(UTC)
    if existente is None:
        existente = Consentimiento(cliente_id=cliente_id, tipo=payload.tipo.value)
        db.add(existente)
    existente.estado = EstadoConsentimiento.OTORGADO.value
    existente.version_texto = payload.version_texto
    existente.fecha_otorgamiento = ahora
    existente.fecha_revocacion = None
    db.add(
        ConsentimientoRegistro(
            cliente_id=cliente_id,
            tipo=payload.tipo.value,
            evento=EstadoConsentimiento.OTORGADO.value,
            version_texto=payload.version_texto,
            canal=payload.canal.value,
            fecha=ahora,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Otro reintento concurrente ganó la inserción: se responde con el vigente.
        db.rollback()
        response.status_code = 200
        return _salida(cliente_id, payload.tipo, consentimiento_vigente(db, cliente_id, payload.tipo))
    db.refresh(existente)
    return _salida(cliente_id, payload.tipo, existente)


@router.get("/{tipo}", response_model=ConsentimientoOut)
def consultar_consentimiento(
    cliente_id: str,
    tipo: TipoConsentimiento,
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
) -> ConsentimientoOut:
    if not acceso.es_servicio:
        exigir_cliente(cliente_id, acceso)
    if db.get(Cliente, cliente_id) is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _salida(cliente_id, tipo, consentimiento_vigente(db, cliente_id, tipo))
