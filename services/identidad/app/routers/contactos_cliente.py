"""Contacto del cliente para servicios internos (HU22): el correo al que el servicio de Notificaciones envía la confirmación."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import es_clave_de_servicio
from app.db import get_db
from app.models import Cliente
from app.schemas import ContactoClienteOut

router = APIRouter(prefix="/v1/contactos-cliente", tags=["contactos-cliente"])


@router.get("/{cliente_id}", response_model=ContactoClienteOut)
def contacto_cliente(
    cliente_id: str,
    x_service_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ContactoClienteOut:
    """Nombre y correo del cliente. Interno: exige la clave de servicio y no sale por el borde público.

    Es un endpoint aparte de `/v1/resumenes-cliente` a propósito: aquel se diseñó (HU20) para no exponer el correo, y
    el documento de la póliza no lo necesita. Aquí no sale el documento ni el celular: el correo es lo único que pide
    el envío, y el nombre sirve para el saludo.
    """
    if not x_service_key or not es_clave_de_servicio(x_service_key):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return ContactoClienteOut(cliente_id=cliente.id, nombre=cliente.nombre, email=cliente.email)
