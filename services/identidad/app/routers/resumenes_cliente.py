"""Resumen del cliente para servicios internos (HU20): lo que el documento de la póliza necesita de él."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import es_clave_de_servicio
from app.db import get_db
from app.models import Cliente
from app.schemas import ResumenClienteOut

router = APIRouter(prefix="/v1/resumenes-cliente", tags=["resumenes-cliente"])

VISIBLES = 3


def enmascarar_documento(numero: str) -> str:
    """Deja visibles solo los últimos dígitos: `100200300` -> `******300`."""
    if len(numero) <= VISIBLES:
        return "*" * len(numero)
    return "*" * (len(numero) - VISIBLES) + numero[-VISIBLES:]


@router.get("/{cliente_id}", response_model=ResumenClienteOut)
def resumen_cliente(
    cliente_id: str,
    x_service_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ResumenClienteOut:
    """Nombre, apellido y documento enmascarado del cliente. Interno: exige la clave de servicio y no sale por el borde público.

    No se reutiliza `/v1/clientes/busqueda`: esa búsqueda es por documento y enmascara el apellido por diseño (HU38).
    El documento sale enmascarado porque el PDF de la póliza solo necesita identificar al cliente, no revelar su número.
    """
    if not x_service_key or not es_clave_de_servicio(x_service_key):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return ResumenClienteOut(
        cliente_id=cliente.id,
        nombre=cliente.nombre,
        apellido=cliente.apellido,
        tipo_documento=cliente.tipo_documento,
        documento_enmascarado=enmascarar_documento(cliente.numero_documento),
    )
