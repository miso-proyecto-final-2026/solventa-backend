"""Venta asistida (HU39): el asesor inicia y sigue la saga de sus cotizaciones y gestiona la firma con el cliente presente.

Rutas propias con el JWT del asesor (ADR-0020). Debajo usan los mismos servicios de HU15 y HU21; el contrato del cliente no cambia.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion.acceso import AsesorActual
from app.suscripcion.api import (
    FirmaResumenOut,
    PolizaResumenOut,
    SuscripcionOut,
    _decidir_en_segundo_plano,
    _firma_resumen,
    a_salida,
)
from app.suscripcion.api_firma import (
    DocumentoFirmaOut,
    FirmaOut,
    _ip,
    _salida,
    _salida_documento,
)
from app.suscripcion.dominio import PASOS, EstadoSaga
from app.suscripcion.firma import OrigenFirma
from app.suscripcion.firma_asistida import ServicioFirmaAsistida, SuscripcionAsistida
from app.suscripcion.modelos_db import FirmaDB

router = APIRouter(prefix="/v1/suscripciones/asistidas", tags=["venta-asistida"])


def obtener_servicio_asistida(request: Request) -> ServicioFirmaAsistida:
    return request.app.state.servicio_asistida


Asistida = Annotated[ServicioFirmaAsistida, Depends(obtener_servicio_asistida)]


class SuscripcionAsistidaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cotizacion_id: str = Field(min_length=1, max_length=36)
    metodo_pago: str = Field(min_length=1, max_length=200, description="Token de la pasarela; nunca el número de tarjeta")
    acepta_terminos: bool = Field(description="El asesor declara que el cliente está al tanto de la transacción y aceptó los términos")
    periodicidad_pago: Literal["ANUAL", "MENSUAL"] = "ANUAL"


class ClienteResumenOut(BaseModel):
    """Lo mínimo para identificar al cliente (ADR-0008): sin apellido completo, documento completo ni datos de contacto."""

    nombre: str
    inicial_apellido: str
    tipo_documento: str
    documento_enmascarado: str


class CompensacionOut(BaseModel):
    estado: str  # SOLICITADA | COMPLETADA
    motivo: str | None
    solicitada_en: datetime | None


class SuscripcionAsistidaOut(BaseModel):
    id: str
    cotizacion_id: str
    cliente: ClienteResumenOut | None = Field(description="Nulo si MS Identidad no responde")
    producto: str
    moneda: str
    monto_asegurado: int
    periodicidad_pago: str
    estado_saga: str
    paso_actual: str
    pasos: list[str]
    decision: str | None
    motivos: list[str]
    cobro_estado: str | None
    cobro_motivo: str | None
    firma: FirmaResumenOut | None
    compensacion: CompensacionOut | None
    poliza: PolizaResumenOut | None
    puede_gestionar_firma: bool = Field(description="Lo calcula el backend; la firma se revalida al ejecutarla")
    creada_en: datetime


class PaginaAsistidasOut(BaseModel):
    items: list[SuscripcionAsistidaOut]
    total: int
    pagina: int
    tamano: int


class FirmaAsistidaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documento_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    documento_revisado: bool
    codigo: str = Field(min_length=1, max_length=12, description="Código de un solo uso del cliente (OTP)")


class FirmaAsistidaOut(FirmaOut):
    modalidad: str
    asesor_id: str | None


def _salida_asistida(a: SuscripcionAsistida) -> SuscripcionAsistidaOut:
    s, p, c = a.suscripcion, a.poliza, a.cliente
    return SuscripcionAsistidaOut(
        id=s.id,
        cotizacion_id=s.cotizacion_id,
        cliente=None
        if c is None
        else ClienteResumenOut(
            nombre=c.nombre,
            inicial_apellido=c.apellido[:1],
            tipo_documento=c.tipo_documento,
            documento_enmascarado=c.documento_enmascarado,
        ),
        producto=s.producto,
        moneda=s.moneda,
        monto_asegurado=int(s.monto_asegurado),
        periodicidad_pago=s.periodicidad_pago,
        estado_saga=s.estado_saga,
        paso_actual=s.paso_actual,
        pasos=PASOS,
        decision=s.decision,
        motivos=list(s.motivos or []),
        cobro_estado=s.cobro_estado,
        cobro_motivo=s.cobro_motivo,
        firma=_firma_resumen(s),
        compensacion=None
        if s.compensacion_estado is None
        else CompensacionOut(
            estado=s.compensacion_estado, motivo=s.compensacion_motivo, solicitada_en=s.compensacion_solicitada_en
        ),
        poliza=None
        if p is None
        else PolizaResumenOut(
            numero_poliza=p.numero_poliza,
            estado=p.estado,
            documento_estado=p.documento_estado,
            documento_intentos=p.documento_intentos,
            documento_error=p.documento_error,
        ),
        puede_gestionar_firma=a.gestionable,
        creada_en=s.created_at,
    )


def _salida_firma(firma: FirmaDB, estado_saga: str) -> FirmaAsistidaOut:
    return FirmaAsistidaOut(**_salida(firma, estado_saga).model_dump(), modalidad=firma.modalidad, asesor_id=firma.asesor_id)


@router.post("", status_code=202, response_model=SuscripcionOut, summary="Inicia la saga de una cotización del asesor")
async def iniciar(
    cuerpo: SuscripcionAsistidaIn,
    request: Request,
    response: Response,
    servicio: Asistida,
    asesor: AsesorActual,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> SuscripcionOut:
    susc, creada = await servicio.iniciar(
        asesor,
        clave=idempotency_key,
        cotizacion_id=cuerpo.cotizacion_id,
        metodo_pago=cuerpo.metodo_pago,
        acepta_terminos=cuerpo.acepta_terminos,
        periodicidad_pago=cuerpo.periodicidad_pago,
    )
    if creada:
        _decidir_en_segundo_plano(request, susc.id)
    else:
        response.headers["Idempotent-Replay"] = "true"
    return a_salida(susc, revision=False)


@router.get("", response_model=PaginaAsistidasOut, summary="Suscripciones de las cotizaciones del asesor")
async def listar(
    servicio: Asistida,
    asesor: AsesorActual,
    estado: EstadoSaga | None = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PaginaAsistidasOut:
    filas, total = await servicio.listar(asesor, estado=estado.value if estado else None, pagina=pagina, tamano=tamano)
    return PaginaAsistidasOut(items=[_salida_asistida(a) for a in filas], total=total, pagina=pagina, tamano=tamano)


@router.get("/{suscripcion_id}", response_model=SuscripcionAsistidaOut, summary="Detalle de la saga para el asesor")
async def detalle(suscripcion_id: str, servicio: Asistida, asesor: AsesorActual) -> SuscripcionAsistidaOut:
    return _salida_asistida(await servicio.detalle(suscripcion_id, asesor))


@router.get("/{suscripcion_id}/documento-firma", response_model=DocumentoFirmaOut, summary="Documento por firmar (hash y vencimiento)")
async def documento_firma(suscripcion_id: str, servicio: Asistida, asesor: AsesorActual) -> DocumentoFirmaOut:
    return _salida_documento(await servicio.documento_para_firmar(suscripcion_id, asesor))


@router.get(
    "/{suscripcion_id}/documento-firma/pdf",
    summary="PDF de la póliza por firmar",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def documento_firma_pdf(suscripcion_id: str, servicio: Asistida, asesor: AsesorActual) -> Response:
    contenido = await servicio.contenido_documento(suscripcion_id, asesor)
    return Response(content=contenido, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post("/{suscripcion_id}/firma/codigo", status_code=202, summary="Envía el código de un solo uso al cliente")
async def solicitar_codigo(suscripcion_id: str, servicio: Asistida, asesor: AsesorActual) -> Response:
    await servicio.solicitar_codigo(suscripcion_id, asesor)
    return Response(status_code=202)


@router.post(
    "/{suscripcion_id}/firma",
    response_model=FirmaAsistidaOut,
    summary="Firma en pantalla con el cliente presente; repetirla devuelve la misma firma",
)
async def firmar(
    suscripcion_id: str,
    cuerpo: FirmaAsistidaIn,
    request: Request,
    response: Response,
    servicio: Asistida,
    asesor: AsesorActual,
    user_agent: Annotated[str | None, Header()] = None,
) -> FirmaAsistidaOut:
    firma, creada = await servicio.firmar(
        suscripcion_id,
        asesor,
        documento_sha256=cuerpo.documento_sha256,
        documento_revisado=cuerpo.documento_revisado,
        codigo=cuerpo.codigo,
        origen=OrigenFirma(canal="WEB", ip=_ip(request), dispositivo=user_agent),
    )
    if not creada:
        response.headers["Idempotent-Replay"] = "true"
    return _salida_firma(firma, await servicio.estado_saga(suscripcion_id))


@router.get(
    "/{suscripcion_id}/firma/documento",
    summary="Descarga la póliza firmada (PDF)",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def descargar_firmada(suscripcion_id: str, servicio: Asistida, asesor: AsesorActual) -> Response:
    contenido, numero = await servicio.copia_firmada(suscripcion_id, asesor)
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="poliza-{numero}-firmada.pdf"', "Cache-Control": "no-store"},
    )
