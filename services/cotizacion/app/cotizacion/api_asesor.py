"""Endpoints de la cotización asistida por un asesor (HU38), el linaje del perfil (HU24) y la
oferta de vida hipotecario (HU26).

Todo el router exige el rol de asesor. El precio lo calcula el mismo motor que usa móvil
(`ServicioCotizacion.cotizar`): aquí solo se identifica quién cotiza y a nombre de quién.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.acceso import AsesorActual, exigir_asesor
from app.cotizacion.api import (
    _ERROR_422,
    CotizacionOut,
    ErrorRespuestaOut,
    RutaCotizacion,
    Servicio,
    a_salida,
)
from app.cotizacion.dominio import (
    CANAL_WEB_ASESOR,
    OrigenCotizacion,
    SolicitudCotizacion,
    fuente_de_perfil,
)
from app.cotizacion.errores import LinajeNoDisponible
from app.cotizacion.linaje import LinajePort
from app.cotizacion.niveles import ClasificadorSenales
from app.cotizacion.oferta import OfertaPort
from app.cotizacion.perfil import PerfilPort

router = APIRouter(
    prefix="/v1/asesor",
    tags=["asesor"],
    route_class=RutaCotizacion,
    dependencies=[Depends(exigir_asesor)],
    responses={
        401: {
            "model": ErrorRespuestaOut,
            "description": "Falta el token del asesor, no es válido o venció.",
        },
        403: {"model": ErrorRespuestaOut, "description": "El token no es de un asesor."},
    },
)

ClienteIdRuta = Annotated[str, Path(min_length=1, max_length=36)]


class SenalNivelOut(BaseModel):
    nombre: str
    nivel: str  # BAJO | MEDIO | ALTO


class PerfilAsesorOut(BaseModel):
    """Resumen del perfil para el asesor: niveles, nunca las cifras del cliente (ADR-0006)."""

    cliente_id: str
    con_consentimiento: bool
    perfil_fuente: str  # REAL | POR_DEFECTO | DEGRADADO
    fuentes: list[str]
    completitud: float
    senales: list[SenalNivelOut]
    obtenido_en: datetime


class DocumentoIn(BaseModel):
    tipo: str = Field(min_length=1, max_length=20)
    numero: str = Field(min_length=1, max_length=20)


class SolicitudAsistidaIn(BaseModel):
    """O un cliente existente (`cliente_id`) o un prospecto sin cuenta (`documento`)."""

    cliente_id: str | None = Field(default=None, min_length=1, max_length=36)
    documento: DocumentoIn | None = None
    producto: str = Field(min_length=1, max_length=50)
    datos: dict[str, object]

    @model_validator(mode="after")
    def _uno_solo(self) -> Self:
        if (self.cliente_id is None) == (self.documento is None):
            raise ValueError("Indique exactamente uno de cliente_id o documento")
        return self


class LineaLinajeAsesorOut(BaseModel):
    fuente: str
    tipo_dato: str
    campos: list[str]
    obtenido_en: datetime | None
    ttl_s: int | None
    hit_cache: bool
    degradado: bool
    por_defecto: bool
    estado_consentimiento: str


class ConflictoLinajeOut(BaseModel):
    campo: str
    ganadora: str
    descartadas: list[str]


class LinajeAsesorOut(BaseModel):
    """Linaje del último perfil enriquecido: fuentes, fechas y marcas, nunca valores (HU24)."""

    perfil_id: str
    cliente_id: str
    creado_en: datetime
    consentimiento_id: str | None
    fuentes: list[LineaLinajeAsesorOut]
    ausentes: dict[str, str]
    conflictos: list[ConflictoLinajeOut]


class OfertaVidaHipotecarioIn(BaseModel):
    """Sin suma asegurada, Perfilamiento usa la de sus reglas (ingreso por meses, HU25)."""

    suma_asegurada: int | None = Field(default=None, gt=0, le=10_000_000_000)
    # Al abrir el flujo se enriquece el perfil (criterio 1); al solo cambiar la suma, no hace falta.
    enriquecer: bool = True


class CoberturaOfertaOut(BaseModel):
    tipo: str
    monto: int
    deducible: int


class AjusteOfertaOut(BaseModel):
    """Un paso del precio: qué regla se aplicó y cuánto movió la prima, sin la cifra del cliente (ADR-0006)."""

    paso: str
    nombre: str
    factor: str | None
    ajuste: int
    acumulado: int
    sin_dato: bool


class OfertaVidaHipotecarioOut(BaseModel):
    """La oferta de Perfilamiento (HU25), ya registrada como cotización para la suscripción asistida."""

    cotizacion_id: str
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
    perfil_creado_en: datetime
    degradado: bool
    coberturas: list[CoberturaOfertaOut]
    ajustes: list[AjusteOfertaOut]
    vigente_hasta: datetime
    creada_en: datetime
    asesor_id: str
    canal: str


class CotizacionAsistidaOut(CotizacionOut):
    perfil_fuente: str
    canal: str
    sin_cuenta: bool


@router.get(
    "/clientes/{cliente_id}/perfil",
    response_model=PerfilAsesorOut,
    summary="Resumen del perfil del cliente para la venta asistida",
)
async def perfil_del_cliente(
    cliente_id: ClienteIdRuta, request: Request
) -> PerfilAsesorOut:
    perfiles: PerfilPort = request.app.state.perfiles
    clasificador: ClasificadorSenales = request.app.state.clasificador_senales
    # Pasa por el mismo camino que la cotización (consentimiento, caché, breaker, trazabilidad):
    # deja el perfil en caché y la cotización siguiente sale más rápido.
    perfil = await perfiles.obtener(cliente_id)
    return PerfilAsesorOut(
        cliente_id=cliente_id,
        con_consentimiento=perfil.con_consentimiento,
        perfil_fuente=fuente_de_perfil(
            degradado=perfil.degradado, aproximada=not perfil.con_consentimiento
        ),
        fuentes=list(perfil.fuentes),
        completitud=perfil.completitud,
        senales=[
            SenalNivelOut(nombre=n, nivel=nivel)
            for n, nivel in clasificador.clasificar_todas(perfil.senales)
        ],
        obtenido_en=datetime.now(UTC),
    )


@router.get(
    "/clientes/{cliente_id}/linaje",
    response_model=LinajeAsesorOut,
    summary="Linaje de fuentes del último perfil enriquecido del cliente",
    responses={
        404: {"model": ErrorRespuestaOut, "description": "El cliente no tiene un perfil enriquecido."},
        503: {"model": ErrorRespuestaOut, "description": "El servicio de perfilamiento no está disponible."},
        **_ERROR_422,
    },
)
async def linaje_del_cliente(
    cliente_id: ClienteIdRuta, request: Request, asesor: AsesorActual
) -> LinajeAsesorOut:
    linajes: LinajePort = request.app.state.linajes
    # Perfilamiento registra la consulta a nombre del asesor; no exige consentimiento (V-12, #106).
    cuerpo = await linajes.obtener(cliente_id, asesor.id)
    try:
        return LinajeAsesorOut.model_validate(cuerpo)
    except ValidationError as exc:
        # Se filtra lo que no esté en el contrato y se rechaza lo que no lo cumpla.
        raise LinajeNoDisponible from exc


@router.post(
    "/clientes/{cliente_id}/ofertas-vida-hipotecario",
    status_code=201,
    response_model=OfertaVidaHipotecarioOut,
    summary="Genera la oferta de vida hipotecario del cliente y la registra como cotización",
    responses={
        404: {"model": ErrorRespuestaOut, "description": "El cliente no tiene un perfil enriquecido."},
        409: {"model": ErrorRespuestaOut, "description": "El cliente no tiene consentimiento vigente."},
        503: {"model": ErrorRespuestaOut, "description": "El servicio de perfilamiento no está disponible."},
        **_ERROR_422,
    },
)
async def oferta_vida_hipotecario(
    cliente_id: ClienteIdRuta,
    cuerpo: OfertaVidaHipotecarioIn,
    request: Request,
    servicio: Servicio,
    asesor: AsesorActual,
) -> OfertaVidaHipotecarioOut:
    ofertas: OfertaPort = request.app.state.ofertas
    if cuerpo.enriquecer:
        await ofertas.enriquecer(cliente_id, asesor.id)
    oferta = await ofertas.calcular(cliente_id, asesor.id, cuerpo.suma_asegurada)
    origen = OrigenCotizacion(canal=CANAL_WEB_ASESOR, asesor_id=asesor.id)
    emitida = await servicio.registrar_oferta(oferta, origen)
    return OfertaVidaHipotecarioOut(
        cotizacion_id=emitida.id,
        oferta_id=oferta.oferta_id,
        cliente_id=emitida.cliente_id,
        producto=emitida.producto,
        moneda=emitida.moneda,
        suma_asegurada=oferta.suma_asegurada,
        prima_anual=oferta.prima_anual,
        prima_mensual=oferta.prima_mensual,
        etapa_de_vida=oferta.etapa_de_vida,
        version_reglas=oferta.version_reglas,
        perfil_id=oferta.perfil_id,
        perfil_creado_en=oferta.perfil_creado_en,
        degradado=oferta.degradado,
        coberturas=[
            CoberturaOfertaOut(tipo=c.tipo, monto=int(c.monto), deducible=int(c.deducible))
            for c in emitida.coberturas
        ],
        ajustes=[
            AjusteOfertaOut(
                paso=p.paso,
                nombre=p.nombre,
                factor=p.factor,
                ajuste=p.ajuste,
                acumulado=p.acumulado,
                sin_dato=p.sin_dato,
            )
            for p in oferta.desglose
        ],
        vigente_hasta=emitida.vigente_hasta,
        creada_en=emitida.creada_en,
        asesor_id=asesor.id,
        canal=origen.canal,
    )


@router.post(
    "/cotizaciones",
    status_code=201,
    response_model=CotizacionAsistidaOut,
    summary="Calcula y emite una cotización a nombre de un cliente (asesor)",
    responses=_ERROR_422,
)
async def crear_cotizacion_asistida(
    cuerpo: SolicitudAsistidaIn, servicio: Servicio, asesor: AsesorActual
) -> CotizacionAsistidaOut:
    if cuerpo.documento is not None:
        # Prospecto sin cuenta: id nuevo y perfil por defecto; se guarda el documento consultado.
        cliente_id = str(uuid.uuid4())
        origen = OrigenCotizacion(
            canal=CANAL_WEB_ASESOR,
            asesor_id=asesor.id,
            sin_cuenta=True,
            documento_tipo=cuerpo.documento.tipo,
            documento_numero=cuerpo.documento.numero,
        )
    else:
        cliente_id = cuerpo.cliente_id or ""
        origen = OrigenCotizacion(canal=CANAL_WEB_ASESOR, asesor_id=asesor.id)
    emitida = await servicio.cotizar(
        SolicitudCotizacion(
            cliente_id=cliente_id,
            producto=cuerpo.producto,
            datos=cuerpo.datos,
            origen=origen,
        )
    )
    return CotizacionAsistidaOut(
        **(a_salida(emitida).model_dump() | {"asesor_id": origen.asesor_id, "sin_cuenta": origen.sin_cuenta}),
        perfil_fuente=emitida.perfil_fuente,
        canal=origen.canal,
    )
