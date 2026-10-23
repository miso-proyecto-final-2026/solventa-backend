"""Validación de sesión para servicios internos (HU15): de quién es el token de un cliente."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import cliente_de_token, es_clave_de_servicio
from app.db import get_db
from app.schemas import SesionActualOut

router = APIRouter(prefix="/v1/sesion", tags=["sesion"])


@router.get("/actual", response_model=SesionActualOut)
def sesion_actual(
    authorization: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> SesionActualOut:
    """Devuelve el `cliente_id` dueño del Bearer. Interno: exige la clave de servicio y no sale por el borde público.

    Un servicio que recibe el token de un cliente (p. ej. Suscripción antes de crear una saga de cobro) lo
    reenvía aquí en lugar de confiar en el `cliente_id` que el cliente escriba en la solicitud.
    """
    if not x_service_key or not es_clave_de_servicio(x_service_key):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Se requiere el token del cliente")
    cliente_id = cliente_de_token(db, authorization[7:].strip())
    if cliente_id is None:
        raise HTTPException(status_code=401, detail="Sesión no válida")
    return SesionActualOut(cliente_id=cliente_id)
