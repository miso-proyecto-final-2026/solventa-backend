"""API de Perfilamiento (HU23). Acceso por clave de servicio en `X-Service-Key`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from app.perfilamiento.errores import NoAutenticado, SinPermiso
from app.perfilamiento.servicio import Enriquecedor
from app.seguridad import clave_valida


def exigir_servicio(
    request: Request, x_service_key: Annotated[str | None, Header()] = None
) -> str:
    """Devuelve el nombre del llamador (el «origen» de los intentos bloqueados).

    401 sin cabecera, 403 con una clave desconocida. Sin claves configuradas nadie accede.
    """
    if not x_service_key:
        raise NoAutenticado
    claves: dict[str, str] = request.app.state.api_keys
    if not clave_valida(x_service_key, claves):
        raise SinPermiso
    return next(nombre for clave, nombre in claves.items() if clave_valida(x_service_key, [clave]))


Origen = Annotated[str, Depends(exigir_servicio)]


def obtener_enriquecedor(request: Request) -> Enriquecedor:
    return request.app.state.enriquecedor


class CampoOut(BaseModel):
    valor: float | int
    fuente: str
    obtenido_en: str | None


class PerfilOut(BaseModel):
    perfil_id: str
    cliente_id: str
    creado_en: str
    consentimiento_id: str | None
    campos: dict[str, CampoOut]
    ausentes: dict[str, str]
    conflictos: list[dict[str, Any]]
    completitud: float
    degradado: bool
    con_consentimiento: bool
    hit_cache: bool
    fuentes: list[str]


class LineaLinajeOut(BaseModel):
    fuente: str
    campos: list[str]
    obtenido_en: str | None
    ttl_s: int | None
    hit_cache: bool
    degradado: bool
    por_defecto: bool


class LinajeOut(BaseModel):
    perfil_id: str
    cliente_id: str
    creado_en: str
    consentimiento_id: str | None
    fuentes: list[LineaLinajeOut]


router = APIRouter(prefix="/v1/perfilamiento", tags=["perfilamiento"])
EnriquecedorActual = Annotated[Enriquecedor, Depends(obtener_enriquecedor)]


@router.post(
    "/{cliente_id}/enriquecer",
    response_model=PerfilOut,
    status_code=201,
    summary="Enriquecer el perfil del cliente con Open Finance y Open Data",
)
async def enriquecer(cliente_id: str, origen: Origen, servicio: EnriquecedorActual) -> dict[str, Any]:
    return (await servicio.enriquecer(cliente_id, origen)).cuerpo


@router.get(
    "/{cliente_id}/perfil",
    response_model=PerfilOut,
    summary="Perfil enriquecido vigente (exige consentimiento vigente)",
)
async def perfil(cliente_id: str, origen: Origen, servicio: EnriquecedorActual) -> dict[str, Any]:
    return (await servicio.perfil_vigente(cliente_id, origen)).cuerpo


@router.get(
    "/{cliente_id}/linaje",
    response_model=LinajeOut,
    summary="Linaje del perfil vigente, para auditoría",
)
async def linaje(cliente_id: str, _origen: Origen, servicio: EnriquecedorActual) -> LinajeOut:
    l = await servicio.linaje(cliente_id)
    return LinajeOut(
        perfil_id=l.perfil_id,
        cliente_id=l.cliente_id,
        creado_en=l.creado_en.isoformat(),
        consentimiento_id=l.consentimiento_id,
        fuentes=[
            LineaLinajeOut(
                fuente=f.fuente,
                campos=list(f.campos),
                obtenido_en=f.obtenido_en.isoformat() if f.obtenido_en else None,
                ttl_s=f.ttl_s,
                hit_cache=f.hit_cache,
                degradado=f.degradado,
                por_defecto=f.por_defecto,
            )
            for f in l.fuentes
        ],
    )
