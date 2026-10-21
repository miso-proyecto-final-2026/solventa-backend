from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import obtener_emisor
from app.db import get_db
from app.models import UsuarioInterno
from app.schemas import LoginInternoIn, SesionInternaOut, UsuarioInternoOut
from app.security import hash_password, verify_password
from app.tokens import EmisorToken

router = APIRouter(prefix="/v1/sesiones-internas", tags=["sesiones-internas"])

MENSAJE_CREDENCIALES = "Correo o contraseña incorrectos"


@lru_cache
def _hash_ficticio() -> str:
    return hash_password("ficticio-para-igualar-tiempos")


@router.post("", response_model=SesionInternaOut)
def iniciar_sesion(
    payload: LoginInternoIn,
    db: Session = Depends(get_db),
    emisor: EmisorToken = Depends(obtener_emisor),
) -> SesionInternaOut:
    usuario = db.query(UsuarioInterno).filter(UsuarioInterno.email == payload.email.strip().lower()).first()
    # Se verifica siempre una contraseña, aunque el usuario no exista, para no filtrar cuentas por tiempo.
    valida = verify_password(payload.password, usuario.password_hash if usuario else _hash_ficticio())
    if usuario is None or not usuario.activo or not valida:
        raise HTTPException(status_code=401, detail=MENSAJE_CREDENCIALES)
    token = emisor.emitir(usuario.id, usuario.rol, usuario.nombre)
    return SesionInternaOut(
        access_token=token,
        expira_en=emisor.ttl_s,
        usuario=UsuarioInternoOut(id=usuario.id, nombre=usuario.nombre, rol=usuario.rol),
    )
