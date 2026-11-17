"""API de Perfilamiento (HU23, HU24). Acceso por clave de servicio en `X-Service-Key`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from app.perfilamiento.errores import NoAutenticado, SinPermiso
from app.perfilamiento.servicio import (
    ESTADO_CONSENTIMIENTO_AL_CONSULTAR,
    TIPO_DATO,
    Enriquecedor,
)
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
    tipo_dato: str = Field(description="FINANCIERO (Open Finance) o SOCIOECONOMICO (Open Data).")
    campos: list[str] = Field(description="Nombres de los campos que aportó la fuente, nunca sus valores.")
    obtenido_en: str | None = Field(
        description="Cuándo se consultó al proveedor. Si salió de caché, la fecha de la consulta original."
    )
    ttl_s: int | None
    hit_cache: bool
    degradado: bool
    por_defecto: bool
    estado_consentimiento: str = Field(
        description="Estado del consentimiento al consultar la fuente: siempre OTORGADO, porque sin él no se enriquece."
    )


class ConflictoOut(BaseModel):
    campo: str
    ganadora: str
    descartadas: list[str]


class LinajeOut(BaseModel):
    perfil_id: str
    cliente_id: str
    creado_en: str
    consentimiento_id: str | None
    fuentes: list[LineaLinajeOut]
    ausentes: dict[str, str] = Field(description="Campo que faltó → motivo. Sin valores.")
    conflictos: list[ConflictoOut] = Field(description="Campos que dieron dos fuentes y cuál se usó.")


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
    summary="Linaje de un perfil (por defecto, el vigente), para auditoría",
    description=(
        "Fuentes, tipo de dato, fechas, TTL y marcas de caché o degradación, sin valores. "
        "Cada consulta queda registrada con el servicio que llamó y, si lo informa en "
        "`X-Consultado-Por`, la persona a cuyo nombre consulta."
    ),
)
async def linaje(
    cliente_id: str,
    origen: Origen,
    servicio: EnriquecedorActual,
    perfil_id: Annotated[
        str | None, Query(min_length=1, max_length=36, description="Un perfil concreto del cliente.")
    ] = None,
    x_consultado_por: Annotated[
        str | None,
        Header(description="Id de la persona a cuyo nombre consulta el servicio (p. ej., el asesor)."),
    ] = None,
) -> LinajeOut:
    l = await servicio.linaje(cliente_id, origen, x_consultado_por, perfil_id)
    return LinajeOut(
        perfil_id=l.perfil_id,
        cliente_id=l.cliente_id,
        creado_en=l.creado_en.isoformat(),
        consentimiento_id=l.consentimiento_id,
        fuentes=[
            LineaLinajeOut(
                fuente=f.fuente,
                tipo_dato=TIPO_DATO.get(f.fuente, "DESCONOCIDO"),
                campos=list(f.campos),
                obtenido_en=f.obtenido_en.isoformat() if f.obtenido_en else None,
                ttl_s=f.ttl_s,
                hit_cache=f.hit_cache,
                degradado=f.degradado,
                por_defecto=f.por_defecto,
                estado_consentimiento=ESTADO_CONSENTIMIENTO_AL_CONSULTAR,
            )
            for f in l.fuentes
        ],
        ausentes=l.ausentes,
        conflictos=[ConflictoOut(**c) for c in l.conflictos],
    )
