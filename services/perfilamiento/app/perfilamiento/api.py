"""API de Perfilamiento (HU23). Acceso por clave de servicio en `X-Service-Key`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
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


class PasoOut(BaseModel):
    paso: str
    nombre: str
    factor: str | None
    valor_perfil: float | int | None
    ajuste: int
    acumulado: int
    sin_dato: bool


class CoberturaOut(BaseModel):
    tipo: str
    monto_cubierto: int
    deducible_porcentaje: str


class OfertaOut(BaseModel):
    oferta_id: str
    cliente_id: str
    producto: str
    moneda: str
    suma_asegurada: int
    prima_anual: int
    prima_mensual: int
    etapa_de_vida: str
    version_reglas: str
    perfil_id: str
    perfil_creado_en: str
    degradado: bool
    desglose: list[PasoOut]
    coberturas: list[CoberturaOut]
    creado_en: str


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


@router.get(
    "/{cliente_id}/oferta",
    response_model=OfertaOut,
    summary="Oferta de vida hipotecario con el precio individualizado por el perfil (exige consentimiento vigente)",
    responses={
        404: {"description": "El cliente no tiene perfil enriquecido: no se inventa un precio."},
        422: {"description": "Suma asegurada fuera de los límites de las reglas."},
    },
)
async def oferta(
    cliente_id: str,
    origen: Origen,
    servicio: EnriquecedorActual,
    suma_asegurada: Annotated[
        int | None,
        Query(gt=0, description="Saldo a asegurar en COP. Sin ella se usa el ingreso por los meses de las reglas."),
    ] = None,
) -> OfertaOut:
    guardada, calculada, perfil_creado_en = await servicio.oferta(cliente_id, origen, suma_asegurada)
    return OfertaOut(
        oferta_id=guardada.id,
        cliente_id=guardada.cliente_id,
        producto=guardada.producto,
        moneda=guardada.moneda,
        suma_asegurada=guardada.suma_asegurada,
        prima_anual=guardada.prima_anual,
        prima_mensual=guardada.prima_mensual,
        etapa_de_vida=guardada.etapa_de_vida,
        version_reglas=guardada.version_reglas,
        perfil_id=guardada.perfil_id,
        perfil_creado_en=perfil_creado_en.isoformat(),
        degradado=guardada.degradado,
        desglose=[PasoOut(**p) for p in guardada.desglose],
        coberturas=[CoberturaOut(**c) for c in calculada.coberturas],
        creado_en=guardada.creado_en.isoformat(),
    )
