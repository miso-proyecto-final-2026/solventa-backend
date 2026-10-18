"""Endpoints de cotización (HU12). Cumplen docs/api/cotizacion.openapi.yaml."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from app.cotizacion.dominio import (
    CotizacionEmitida,
    DesglosePrecio,
    SolicitudCotizacion,
)
from app.cotizacion.errores import (
    DetalleError,
    NoAutenticado,
    SinPermiso,
    cuerpo_error,
)
from app.cotizacion.servicio import ServicioCotizacion
from app.seguridad import clave_valida


class RutaCotizacion(APIRoute):
    """Devuelve los errores de forma del cuerpo en el formato estándar de error."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def manejador(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                detalles = [
                    DetalleError(
                        campo=".".join(str(p) for p in e["loc"][1:])
                        or str(e["loc"][0]),
                        motivo=e["msg"],
                    )
                    for e in exc.errors()
                ]
                return JSONResponse(
                    status_code=422,
                    content=cuerpo_error(
                        "VALIDATION_ERROR", "La solicitud no es válida.", detalles
                    ),
                )

        return manejador


router = APIRouter(
    prefix="/v1/cotizaciones", tags=["cotizaciones"], route_class=RutaCotizacion
)


class SolicitudIn(BaseModel):
    cliente_id: str = Field(min_length=1, max_length=36)
    producto: str = Field(min_length=1, max_length=50)
    datos: dict[str, Any]


class CoberturaOut(BaseModel):
    tipo: str
    monto: int
    deducible: int


class ComponenteOut(BaseModel):
    concepto: str
    monto: int


class MetadataOut(BaseModel):
    hit_cache: bool
    latencia_interna_ms: float | None


class CotizacionOut(BaseModel):
    id: str
    cliente_id: str
    producto: str
    estado: str
    moneda: str
    prima_anual: int
    prima_mensual: int
    coberturas: list[CoberturaOut]
    vigente_hasta: datetime
    version_reglas: str
    degradado: bool
    aproximada: bool
    aviso: str | None
    componentes: list[ComponenteOut] | None
    metadata: MetadataOut | None


class FactorOut(BaseModel):
    etapa: str
    nombre: str
    factor: str
    fuente: str | None
    obtenido_en: datetime | None


class PerfilUsadoOut(BaseModel):
    fuentes: list[str]
    degradado: bool
    con_consentimiento: bool
    completitud: float


class DesgloseOut(BaseModel):
    cotizacion_id: str
    version_esquema: int
    version_reglas: str
    producto: str
    moneda: str
    prima_base: int
    factores: list[FactorOut]
    recargo_degradado_pct: str | None
    componentes: list[ComponenteOut]
    prima_anual: int
    perfil: PerfilUsadoOut


class ConsultaExternaOut(BaseModel):
    id: str
    cliente_id: str
    proveedor: str
    categorias: list[str]
    consultado_en: datetime
    resultado: str
    latencia_ms: float
    hit_cache: bool
    consentimiento_id: str | None
    cotizacion_id: str | None
    correlacion_id: str | None


class ConsultasExternasOut(BaseModel):
    cotizacion_id: str
    consultas: list[ConsultaExternaOut]


class LatenciaClienteIn(BaseModel):
    latencia_cliente_ms: float = Field(gt=0)


class DetalleErrorOut(BaseModel):
    campo: str
    motivo: str


class ErrorCuerpoOut(BaseModel):
    code: str
    message: str
    details: list[DetalleErrorOut]
    trace_id: str


class ErrorRespuestaOut(BaseModel):
    error: ErrorCuerpoOut


_ERROR_422 = {422: {"model": ErrorRespuestaOut, "description": "Solicitud no válida."}}
_ERROR_404 = {
    404: {"model": ErrorRespuestaOut, "description": "La cotización no existe."}
}
_ERRORES_DESGLOSE = {
    401: {"model": ErrorRespuestaOut, "description": "Falta la credencial."},
    403: {"model": ErrorRespuestaOut, "description": "Sin permiso de auditoría."},
    404: {
        "model": ErrorRespuestaOut,
        "description": "La cotización no existe o no tiene desglose.",
    },
}


def obtener_servicio(request: Request) -> ServicioCotizacion:
    return request.app.state.servicio_cotizacion


Servicio = Annotated[ServicioCotizacion, Depends(obtener_servicio)]
CotizacionId = Annotated[str, Path(alias="id", min_length=1, max_length=36)]


def exigir_auditor(
    request: Request,
    x_service_key: Annotated[str | None, Header()] = None,
) -> None:
    """Rol de auditor con permisos mínimos: una clave de servicio dedicada (HU14).

    Falla cerrado: sin claves configuradas nadie accede. Solución provisional hasta
    que exista la gestión de roles internos (F26).
    """
    if not x_service_key:
        raise NoAutenticado
    claves = getattr(request.app.state, "auditor_api_keys", ())
    if not clave_valida(x_service_key, claves):
        raise SinPermiso


Auditor = Annotated[None, Depends(exigir_auditor)]


def a_salida_desglose(cotizacion_id: str, d: DesglosePrecio) -> DesgloseOut:
    return DesgloseOut(
        cotizacion_id=cotizacion_id,
        version_esquema=d.version_esquema,
        version_reglas=d.version_reglas,
        producto=d.producto,
        moneda=d.moneda,
        prima_base=int(d.prima_base),
        factores=[
            FactorOut(
                etapa=f.etapa,
                nombre=f.nombre,
                factor=str(f.factor),
                fuente=f.fuente,
                obtenido_en=f.obtenido_en,
            )
            for f in d.factores
        ],
        recargo_degradado_pct=(
            None if d.recargo_degradado_pct is None else str(d.recargo_degradado_pct)
        ),
        componentes=[
            ComponenteOut(concepto=c.concepto, monto=int(c.monto))
            for c in d.componentes
        ],
        prima_anual=int(d.prima_anual),
        perfil=PerfilUsadoOut(
            fuentes=list(d.perfil.fuentes),
            degradado=d.perfil.degradado,
            con_consentimiento=d.perfil.con_consentimiento,
            completitud=d.perfil.completitud,
        ),
    )


def a_salida(c: CotizacionEmitida) -> CotizacionOut:
    return CotizacionOut(
        id=c.id,
        cliente_id=c.cliente_id,
        producto=c.producto,
        estado=c.estado,
        moneda=c.moneda,
        prima_anual=int(c.prima_anual),
        prima_mensual=int(c.prima_mensual),
        coberturas=[
            CoberturaOut(tipo=x.tipo, monto=int(x.monto), deducible=int(x.deducible))
            for x in c.coberturas
        ],
        vigente_hasta=c.vigente_hasta,
        version_reglas=c.version_reglas,
        degradado=c.degradado,
        aproximada=c.aproximada,
        aviso=c.aviso,
        componentes=[
            ComponenteOut(concepto=x.concepto, monto=int(x.monto))
            for x in c.componentes
        ]
        or None,
        metadata=MetadataOut(
            hit_cache=c.hit_cache, latencia_interna_ms=c.latencia_interna_ms
        ),
    )


@router.post(
    "",
    status_code=201,
    response_model=CotizacionOut,
    summary="Calcula y emite una cotización",
    responses=_ERROR_422,
)
async def crear_cotizacion(cuerpo: SolicitudIn, servicio: Servicio) -> CotizacionOut:
    emitida = await servicio.cotizar(
        SolicitudCotizacion(
            cliente_id=cuerpo.cliente_id, producto=cuerpo.producto, datos=cuerpo.datos
        )
    )
    return a_salida(emitida)


@router.get(
    "/{id}",
    response_model=CotizacionOut,
    summary="Consulta una cotización emitida",
    responses=_ERROR_404,
)
async def obtener_cotizacion(
    cotizacion_id: CotizacionId, servicio: Servicio
) -> CotizacionOut:
    return a_salida(await servicio.obtener(cotizacion_id))


@router.get(
    "/{id}/desglose",
    response_model=DesgloseOut,
    summary="Desglose del cálculo del precio (solo auditor)",
    responses=_ERRORES_DESGLOSE,
)
async def obtener_desglose(
    cotizacion_id: CotizacionId, servicio: Servicio, _auditor: Auditor
) -> DesgloseOut:
    return a_salida_desglose(
        cotizacion_id, await servicio.obtener_desglose(cotizacion_id)
    )


@router.get(
    "/{id}/consultas-externas",
    response_model=ConsultasExternasOut,
    summary="Consultas a proveedores que alimentaron la cotización (solo auditor)",
    responses=_ERRORES_DESGLOSE,
)
async def obtener_consultas_externas(
    cotizacion_id: CotizacionId, servicio: Servicio, _auditor: Auditor
) -> ConsultasExternasOut:
    registros = await servicio.consultas_externas(cotizacion_id)
    return ConsultasExternasOut(
        cotizacion_id=cotizacion_id,
        consultas=[
            ConsultaExternaOut(
                id=r.id,
                cliente_id=r.cliente_id,
                proveedor=r.proveedor,
                categorias=list(r.categorias),
                consultado_en=r.consultado_en,
                resultado=r.resultado.value,
                latencia_ms=r.latencia_ms,
                hit_cache=r.hit_cache,
                consentimiento_id=r.consentimiento_id,
                cotizacion_id=r.cotizacion_id,
                correlacion_id=r.correlacion_id,
            )
            for r in registros
        ],
    )


@router.put(
    "/{id}/latencia-cliente",
    status_code=204,
    summary="Registra la latencia medida en el cliente",
    responses={**_ERROR_404, **_ERROR_422},
)
async def registrar_latencia_cliente(
    cotizacion_id: CotizacionId, cuerpo: LatenciaClienteIn, servicio: Servicio
) -> Response:
    await servicio.registrar_latencia_cliente(cotizacion_id, cuerpo.latencia_cliente_ms)
    return Response(status_code=204)
