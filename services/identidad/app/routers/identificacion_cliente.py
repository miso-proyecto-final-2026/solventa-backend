"""Identificación de un cliente por documento para servicios internos (HU33): MS Socios la usa en la venta embebida."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import es_clave_de_servicio
from app.db import get_db
from app.routers.clientes import identificar_cliente
from app.schemas import (
    EstadoConsentimiento,
    IdentificacionClienteIn,
    IdentificacionClienteOut,
)

router = APIRouter(prefix="/v1/interno/clientes", tags=["interno"])


@router.post("/identificacion", response_model=IdentificacionClienteOut)
def identificar(
    payload: IdentificacionClienteIn,
    x_service_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> IdentificacionClienteOut:
    """Resuelve `cliente_id`, KYC y consentimiento vigente. Interno: exige la clave de servicio.

    El documento va en el cuerpo y no en la URL para que no se registre en los logs de acceso. La respuesta no lleva
    datos personales: solo lo necesario para que el socio sepa si puede vender.
    """
    if not x_service_key or not es_clave_de_servicio(x_service_key):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    cliente, consentimiento = identificar_cliente(db, payload.tipo_documento, payload.numero_documento)
    return IdentificacionClienteOut(
        cliente_id=cliente.id,
        estado_kyc=cliente.estado_kyc,
        consentimiento_vigente=consentimiento.estado == EstadoConsentimiento.OTORGADO,
    )
