"""Billetera del cliente (HU20): sus pólizas con estado, vigencia y prima."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.suscripcion.api import PolizaOut, Servicio, _token, poliza_a_salida

router = APIRouter(prefix="/v1/polizas", tags=["polizas"])


class BilleteraOut(BaseModel):
    cliente_id: str
    polizas: list[PolizaOut]


@router.get("", response_model=BilleteraOut, summary="Pólizas del cliente (billetera)")
async def billetera(
    servicio: Servicio,
    cliente_id: Annotated[str, Query(min_length=1, max_length=64)],
    authorization: Annotated[str | None, Header()] = None,
) -> BilleteraOut:
    """Un `cliente_id` distinto al dueño del token es 403."""
    filas = await servicio.billetera(cliente_id, _token(authorization))
    return BilleteraOut(cliente_id=cliente_id, polizas=[poliza_a_salida(p, s) for p, s in filas])
