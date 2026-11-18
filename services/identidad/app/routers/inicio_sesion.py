"""Inicio de sesión de clientes (HU05): correo y contraseña a cambio de un token de sesión nuevo.

Es el respaldo del acceso biométrico (tras 3 fallos, cancelación o cambio de biometrías) y la forma de entrar cuando la
sesión guardada ya no sirve. La respuesta es la misma para un correo que no existe y para una contraseña incorrecta, y
cinco fallos seguidos bloquean 15 minutos.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import acceso
from app.auth import crear_sesion
from app.db import get_db
from app.models import Cliente
from app.routers.clientes import _cliente_out
from app.schemas import ClienteOut, InicioSesionCreate
from app.security import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/clientes", tags=["inicio-de-sesion"])

CREDENCIALES_INVALIDAS = {"codigo": "CREDENCIALES_INVALIDAS", "mensaje": "Correo o contraseña incorrectos"}

# Hash de una contraseña que nadie usa: para un correo que no existe se verifica igual, y la respuesta no tarda menos.
_HASH_SIN_CUENTA = hash_password("sin-cuenta-solventa")


def bloqueado(segundos: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "codigo": "BLOQUEADO",
            "mensaje": "Demasiados intentos. Intenta de nuevo más tarde",
            "reintentar_en_s": segundos,
        },
        headers={"Retry-After": str(segundos)},
    )


@router.post("/sesion", response_model=ClienteOut)
def iniciar_sesion(payload: InicioSesionCreate, db: Session = Depends(get_db)) -> ClienteOut:
    email = payload.email.strip().lower()
    clave = acceso.clave("login", email)
    faltan = acceso.segundos_bloqueado(db, clave)
    if faltan:
        raise bloqueado(faltan)

    cliente = db.query(Cliente).filter(Cliente.email == email).first()
    correcta = verify_password(payload.password, cliente.password_hash if cliente else _HASH_SIN_CUENTA)
    if cliente is None or not correcta:
        faltan = acceso.registrar_fallo(db, clave)
        logger.info("login_cliente resultado=fallido bloqueado=%s", bool(faltan))
        if faltan:
            raise bloqueado(faltan)
        raise HTTPException(status_code=401, detail=CREDENCIALES_INVALIDAS)

    acceso.limpiar(db, clave)
    token = crear_sesion(db, cliente.id)
    db.commit()
    logger.info("login_cliente cliente=%s resultado=exitoso", cliente.id)
    return _cliente_out(cliente, token)
