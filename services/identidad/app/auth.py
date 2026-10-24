"""Autenticación de MS Identidad: sesión de cliente (Bearer) y clave de servicio.

La sesión es la mínima de HU01 (access_token entregado al registrarse, almacenado como hash).
El inicio de sesión biométrico (HU04/HU05) la extenderá; los servicios internos se identifican
con una clave por entorno (`SERVICE_API_KEYS`, separadas por coma).
"""

import hashlib
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Sesion
from app.tokens import ClaimsInternos, EmisorToken, emisor_desde_entorno


@dataclass(frozen=True)
class Acceso:
    cliente_id: str | None  # None cuando quien llama es un servicio interno
    es_servicio: bool = False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def crear_sesion(db: Session, cliente_id: str) -> str:
    """Crea la sesión y devuelve el token en claro (única vez que existe)."""
    token = secrets.token_urlsafe(32)
    db.add(Sesion(cliente_id=cliente_id, token_hash=hash_token(token)))
    return token


def _claves_de_servicio() -> list[str]:
    return [c.strip() for c in os.environ.get("SERVICE_API_KEYS", "").split(",") if c.strip()]


def _es_clave_de_servicio(clave: str) -> bool:
    return any(secrets.compare_digest(clave, valida) for valida in _claves_de_servicio())


def es_clave_de_servicio(clave: str) -> bool:
    return _es_clave_de_servicio(clave)


def cliente_de_token(db: Session, token: str) -> str | None:
    """`cliente_id` dueño de un token de sesión, o None si no existe."""
    sesion = db.query(Sesion).filter(Sesion.token_hash == hash_token(token)).first()
    return sesion.cliente_id if sesion is not None else None


def acceso_actual(
    authorization: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Acceso:
    if x_service_key is not None:
        if _es_clave_de_servicio(x_service_key):
            return Acceso(cliente_id=None, es_servicio=True)
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    if authorization and authorization.lower().startswith("bearer "):
        cliente_id = cliente_de_token(db, authorization[7:].strip())
        if cliente_id is not None:
            return Acceso(cliente_id=cliente_id)
    raise HTTPException(status_code=401, detail="Se requiere iniciar sesión")


def exigir_cliente(cliente_id: str, acceso: Acceso) -> None:
    """Un cliente solo accede a sus propios datos."""
    if acceso.cliente_id != cliente_id:
        raise HTTPException(status_code=403, detail="No autorizado para este cliente")


@lru_cache
def obtener_emisor() -> EmisorToken:
    """Emisor y verificador del token interno; las pruebas lo reemplazan con `dependency_overrides`."""
    return emisor_desde_entorno()


def exigir_rol(rol: str):
    """Dependencia que exige un Bearer JWT interno con el rol dado (HU38).

    401 si falta o no es válido (el front lo trata como sesión vencida); 403 si el rol no alcanza.
    El token opaco de un cliente nunca pasa por aquí.
    """

    def dependencia(
        authorization: str | None = Header(default=None),
        emisor: EmisorToken = Depends(obtener_emisor),
    ) -> ClaimsInternos:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Se requiere iniciar sesión")
        claims = emisor.verificar(authorization[7:].strip())
        if claims is None:
            raise HTTPException(status_code=401, detail="Sesión inválida o vencida")
        if claims.rol != rol:
            raise HTTPException(status_code=403, detail="No autorizado para esta operación")
        return claims

    return dependencia
