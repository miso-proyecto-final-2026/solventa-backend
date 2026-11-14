"""Acceso biométrico del cliente (HU04): el servidor solo registra qué método se activó.

La verificación biométrica es local en el dispositivo (BiometricPrompt + Keystore). Aquí se confirma la contraseña
actual y que el KYC esté aprobado antes de registrar la activación.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app import acceso as limite
from app.auth import Acceso, acceso_actual, exigir_cliente
from app.db import get_db
from app.models import Cliente, RegistroBiometria
from app.routers.inicio_sesion import bloqueado
from app.schemas import BiometriaActivar, BiometriaOut, MetodoBiometrico
from app.security import verify_password

router = APIRouter(prefix="/v1/clientes/{cliente_id}/biometria", tags=["biometria"])

KYC_APROBADO = "VERIFICADO"


def _cliente(db: Session, cliente_id: str) -> Cliente:
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


def _salida(cliente_id: str, registro: RegistroBiometria | None) -> BiometriaOut:
    if registro is None:
        return BiometriaOut(cliente_id=cliente_id, activo=False)
    return BiometriaOut(
        cliente_id=cliente_id,
        activo=registro.activo,
        metodo=MetodoBiometrico(registro.metodo) if registro.activo else None,
        activado_en=registro.activado_en,
        desactivado_en=registro.desactivado_en,
    )


def _registro(db: Session, cliente_id: str) -> RegistroBiometria | None:
    return db.query(RegistroBiometria).filter(RegistroBiometria.cliente_id == cliente_id).first()


@router.get("", response_model=BiometriaOut)
def estado_biometria(
    cliente_id: str, db: Session = Depends(get_db), acceso: Acceso = Depends(acceso_actual)
) -> BiometriaOut:
    exigir_cliente(cliente_id, acceso)
    _cliente(db, cliente_id)
    return _salida(cliente_id, _registro(db, cliente_id))


@router.post("/activar", response_model=BiometriaOut)
def activar_biometria(
    cliente_id: str,
    payload: BiometriaActivar,
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
) -> BiometriaOut:
    exigir_cliente(cliente_id, acceso)
    cliente = _cliente(db, cliente_id)
    if cliente.estado_kyc != KYC_APROBADO:
        raise HTTPException(
            status_code=403,
            detail={"codigo": "KYC_NO_APROBADO", "mensaje": "Debes verificar tu identidad para activar el acceso biométrico"},
        )
    # Misma contraseña que el inicio de sesión, así que el mismo límite: no se puede adivinar probando aquí.
    clave = limite.clave("activar", cliente_id)
    faltan = limite.segundos_bloqueado(db, clave)
    if faltan:
        raise bloqueado(faltan)
    if not verify_password(payload.contrasena, cliente.password_hash):
        faltan = limite.registrar_fallo(db, clave)
        if faltan:
            raise bloqueado(faltan)
        raise HTTPException(
            status_code=403, detail={"codigo": "CONTRASENA_INCORRECTA", "mensaje": "La contraseña no es correcta"}
        )
    limite.limpiar(db, clave)
    ahora = datetime.now(UTC)
    registro = _registro(db, cliente_id)
    if registro is None:
        registro = RegistroBiometria(cliente_id=cliente_id, metodo=payload.metodo.value, activo=True, activado_en=ahora)
        db.add(registro)
    else:
        registro.metodo = payload.metodo.value
        registro.activo = True
        registro.activado_en = ahora
        registro.desactivado_en = None
    db.commit()
    db.refresh(registro)
    return _salida(cliente_id, registro)


@router.delete("", status_code=204)
def desactivar_biometria(
    cliente_id: str, db: Session = Depends(get_db), acceso: Acceso = Depends(acceso_actual)
) -> Response:
    """Idempotente: desactivar lo que ya está desactivado (o nunca se activó) también responde 204."""
    exigir_cliente(cliente_id, acceso)
    _cliente(db, cliente_id)
    registro = _registro(db, cliente_id)
    if registro is not None and registro.activo:
        registro.activo = False
        registro.desactivado_en = datetime.now(UTC)
        db.commit()
    return Response(status_code=204)
