"""Venta embebida del socio (HU33): rutas internas que MS Socios llama a nombre de un socio y de un cliente que ya identificó.

Son internas (`/v1/interno`, regla V15: no salen por el borde público) y exigen la clave de otro servicio. MS Socios es quien
autentica al socio con OAuth2; aquí `socio_client_id` es la atribución y el filtro de aislamiento: un socio solo ve sus ventas.
Debajo usan los mismos servicios de HU15 y HU21; las reglas de decisión, cobro, póliza y firma no se duplican (ADR-0026).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion.api import (
    CobroOut,
    DecisionOut,
    FirmaResumenOut,
    SuscripcionOut,
    _decidir_en_segundo_plano,
    _firma_resumen,
    a_salida,
)
from app.suscripcion.api_certificado import exigir_servicio
from app.suscripcion.api_firma import (
    DocumentoFirmaOut,
    FirmaOut,
    _ip_valida,
    _salida,
    _salida_documento,
)
from app.suscripcion.dominio import PASOS
from app.suscripcion.firma import OrigenFirma
from app.suscripcion.firma_socio import ServicioFirmaSocio, SuscripcionDeSocio
from app.suscripcion.modelos_db import FirmaDB

router = APIRouter(prefix="/v1/interno/socios", tags=["socios"], dependencies=[Depends(exigir_servicio)])

SocioClientId = Annotated[str, Field(min_length=1, max_length=64, description="`client_id` OAuth del socio")]


def obtener_servicio_socio(request: Request) -> ServicioFirmaSocio:
    return request.app.state.servicio_socio


Socio = Annotated[ServicioFirmaSocio, Depends(obtener_servicio_socio)]
SocioQuery = Annotated[str, Query(min_length=1, max_length=64, description="`client_id` OAuth del socio")]


class SuscripcionSocioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socio_client_id: SocioClientId
    cliente_id: str = Field(min_length=1, max_length=64, description="Cliente que MS Socios ya identificó en MS Identidad")
    cotizacion_id: str = Field(min_length=1, max_length=36)
    metodo_pago: str = Field(min_length=1, max_length=200, description="Token de la pasarela; nunca el número de tarjeta")
    acepta_terminos: bool = Field(description="El socio declara que el cliente aceptó; la prueba es la firma con OTP")
    periodicidad_pago: Literal["ANUAL", "MENSUAL"] = "ANUAL"


class PolizaSocioOut(BaseModel):
    """La póliza de la venta: lo que el socio necesita para informarle al cliente. Sin claves de almacenamiento."""

    numero_poliza: str
    estado: str
    vigencia_desde: datetime
    fecha_vencimiento: datetime
    fecha_emision: datetime | None
    documento_estado: str
    documento_sha256: str | None


class SuscripcionSocioOut(BaseModel):
    id: str
    cotizacion_id: str
    estado_saga: str
    paso_actual: str
    pasos: list[str]
    producto: str
    moneda: str
    monto_asegurado: int
    periodicidad_pago: str
    decision: DecisionOut | None
    cobro: CobroOut | None
    firma: FirmaResumenOut | None
    poliza: PolizaSocioOut | None
    creada_en: datetime


class CodigoFirmaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socio_client_id: SocioClientId


class FirmaSocioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socio_client_id: SocioClientId
    documento_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    documento_revisado: bool
    codigo: str = Field(min_length=1, max_length=12, description="Código de un solo uso del cliente (OTP)")
    canal: Literal["MOVIL", "WEB"]
    ip: str | None = Field(default=None, max_length=45, description="IP del cliente, para la evidencia de no repudio")
    dispositivo: str | None = Field(default=None, max_length=200)


class FirmaSocioOut(FirmaOut):
    modalidad: str
    socio_client_id: str | None


def _salida_detalle(d: SuscripcionDeSocio) -> SuscripcionSocioOut:
    base = a_salida(d.suscripcion, revision=False)
    p = d.poliza
    return SuscripcionSocioOut(
        id=base.id,
        cotizacion_id=base.cotizacion_id,
        estado_saga=base.estado_saga,
        paso_actual=base.paso_actual,
        pasos=PASOS,
        producto=base.producto,
        moneda=base.moneda,
        monto_asegurado=base.monto_asegurado,
        periodicidad_pago=base.periodicidad_pago,
        decision=base.decision,
        cobro=base.cobro,
        firma=_firma_resumen(d.suscripcion),
        poliza=None
        if p is None
        else PolizaSocioOut(
            numero_poliza=p.numero_poliza,
            estado=p.estado,
            vigencia_desde=p.vigencia_desde,
            fecha_vencimiento=p.fecha_vencimiento,
            fecha_emision=p.fecha_emision,
            documento_estado=p.documento_estado,
            documento_sha256=p.documento_sha256,
        ),
        creada_en=base.creada_en,
    )


def _salida_firma(firma: FirmaDB, estado_saga: str) -> FirmaSocioOut:
    return FirmaSocioOut(**_salida(firma, estado_saga).model_dump(), modalidad=firma.modalidad, socio_client_id=firma.socio_client_id)


@router.post("/suscripciones", status_code=202, response_model=SuscripcionOut, summary="Inicia la saga de una cotización del cliente")
async def iniciar(
    cuerpo: SuscripcionSocioIn,
    request: Request,
    response: Response,
    servicio: Socio,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> SuscripcionOut:
    susc, creada = await servicio.iniciar(
        cuerpo.socio_client_id,
        cuerpo.cliente_id,
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


@router.get("/suscripciones/{suscripcion_id}", response_model=SuscripcionSocioOut, summary="Estado de la saga y de la póliza")
async def detalle(suscripcion_id: str, socio_client_id: SocioQuery, servicio: Socio) -> SuscripcionSocioOut:
    return _salida_detalle(await servicio.detalle(suscripcion_id, socio_client_id))


@router.get(
    "/suscripciones/{suscripcion_id}/documento-firma",
    response_model=DocumentoFirmaOut,
    summary="Documento por firmar (hash y vencimiento)",
)
async def documento_firma(suscripcion_id: str, socio_client_id: SocioQuery, servicio: Socio) -> DocumentoFirmaOut:
    return _salida_documento(await servicio.documento_para_firmar(suscripcion_id, socio_client_id))


@router.get(
    "/suscripciones/{suscripcion_id}/documento-firma/pdf",
    summary="PDF de la póliza por firmar",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def documento_firma_pdf(suscripcion_id: str, socio_client_id: SocioQuery, servicio: Socio) -> Response:
    contenido = await servicio.contenido_documento(suscripcion_id, socio_client_id)
    return Response(content=contenido, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post("/suscripciones/{suscripcion_id}/firma/codigo", status_code=202, summary="Envía el código de un solo uso al cliente")
async def solicitar_codigo(suscripcion_id: str, cuerpo: CodigoFirmaIn, servicio: Socio) -> Response:
    await servicio.solicitar_codigo(suscripcion_id, cuerpo.socio_client_id)
    return Response(status_code=202)


@router.post(
    "/suscripciones/{suscripcion_id}/firma",
    response_model=FirmaSocioOut,
    summary="Firma del cliente desde la app del socio; repetirla devuelve la misma firma",
)
async def firmar(suscripcion_id: str, cuerpo: FirmaSocioIn, response: Response, servicio: Socio) -> FirmaSocioOut:
    firma, creada = await servicio.firmar(
        suscripcion_id,
        cuerpo.socio_client_id,
        documento_sha256=cuerpo.documento_sha256,
        documento_revisado=cuerpo.documento_revisado,
        codigo=cuerpo.codigo,
        origen=OrigenFirma(canal=cuerpo.canal, ip=_ip_cliente(cuerpo.ip), dispositivo=cuerpo.dispositivo),
    )
    if not creada:
        response.headers["Idempotent-Replay"] = "true"
    return _salida_firma(firma, await servicio.estado_saga(suscripcion_id))


def _ip_cliente(texto: str | None) -> str | None:
    """La IP del cliente que reenvía el socio; un valor que no sea una IP se descarta (igual que en las demás rutas de firma)."""
    return _ip_valida(texto) if texto else None
