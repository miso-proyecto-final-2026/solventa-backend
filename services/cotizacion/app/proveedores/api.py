"""Endpoint interno de consulta de perfil crudo (HU09)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.proveedores.dominio import PerfilCrudo
from app.proveedores.servicio import ServicioPerfil
from app.seguridad import clave_valida


def exigir_clave_de_servicio(
    request: Request,
    x_service_key: Annotated[str | None, Header()] = None,
) -> None:
    """El perfil crudo trae datos financieros: solo lo consultan servicios u operadores
    con una clave dedicada (`PERFILES_API_KEYS`). Falla cerrado: sin claves, nadie accede."""
    if not x_service_key:
        raise HTTPException(status_code=401, detail="Se requiere la clave de servicio")
    claves = request.app.state.servicio_perfil.config.perfiles_api_keys
    if not clave_valida(x_service_key, claves):
        raise HTTPException(status_code=403, detail="La clave no tiene permiso")


router = APIRouter(
    prefix="/perfiles",
    tags=["perfiles"],
    dependencies=[Depends(exigir_clave_de_servicio)],
)


class FuenteOut(BaseModel):
    fuente: str
    payload: dict[str, Any]
    degradado: bool
    por_defecto: bool
    hit_cache: bool


class PerfilCrudoOut(BaseModel):
    cliente_id: str
    por_defecto: bool
    degradado: bool
    fuentes: list[FuenteOut]


def obtener_servicio(request: Request) -> ServicioPerfil:
    return request.app.state.servicio_perfil


@router.get(
    "/{cliente_id}", response_model=PerfilCrudoOut, summary="Perfil crudo por fuente"
)
async def obtener_perfil(
    cliente_id: str, servicio: Annotated[ServicioPerfil, Depends(obtener_servicio)]
) -> PerfilCrudoOut:
    perfil: PerfilCrudo = await servicio.obtener_perfil(cliente_id)
    return PerfilCrudoOut(
        cliente_id=perfil.cliente_id,
        por_defecto=perfil.por_defecto,
        degradado=perfil.degradado,
        fuentes=[
            FuenteOut(
                fuente=f.fuente.value,
                payload=f.payload,
                degradado=f.degradado,
                por_defecto=f.por_defecto,
                hit_cache=f.hit_cache,
            )
            for f in perfil.fuentes
        ],
    )
