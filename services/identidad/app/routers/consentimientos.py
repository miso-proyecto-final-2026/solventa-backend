from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Acceso, acceso_actual, exigir_cliente
from app.db import get_db
from app.models import Cliente, Consentimiento, ConsentimientoRegistro
from app.schemas import (
    CanalConsentimiento,
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


def _ip_origen(request: Request) -> str | None:
    """IP del cliente: la primera de `X-Forwarded-For` (CloudFront y API Gateway la agregan) o la del socket."""
    reenviada = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = reenviada or (request.client.host if request.client else "")
    return ip[:45] or None


@router.delete("/{tipo}", response_model=ConsentimientoOut)
def revocar_consentimiento(
    cliente_id: str,
    tipo: TipoConsentimiento,
    request: Request,
    canal: CanalConsentimiento = CanalConsentimiento.MOVIL,
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
) -> ConsentimientoOut:
    """Revoca el consentimiento (HU07). Solo el propio cliente; idempotente y auditado."""
    exigir_cliente(cliente_id, acceso)
    if db.get(Cliente, cliente_id) is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    # FOR UPDATE: dos revocaciones simultáneas se serializan y solo la primera deja registro de auditoría.
    vigente = (
        db.query(Consentimiento)
        .filter(Consentimiento.cliente_id == cliente_id, Consentimiento.tipo == tipo.value)
        .with_for_update()
        .first()
    )
    if vigente is None:
        raise HTTPException(status_code=404, detail="Consentimiento no encontrado")
    if vigente.estado == EstadoConsentimiento.REVOCADO.value:
        return _salida(cliente_id, tipo, vigente)

    ahora = datetime.now(UTC)
    vigente.estado = EstadoConsentimiento.REVOCADO.value
    vigente.fecha_revocacion = ahora
    db.add(
        ConsentimientoRegistro(
            cliente_id=cliente_id,
            tipo=tipo.value,
            evento=EstadoConsentimiento.REVOCADO.value,
            version_texto=vigente.version_texto,
            canal=canal.value,
            fecha=ahora,
            ip_origen=_ip_origen(request),
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        )
    )
    db.commit()
    db.refresh(vigente)
    return _salida(cliente_id, tipo, vigente)
