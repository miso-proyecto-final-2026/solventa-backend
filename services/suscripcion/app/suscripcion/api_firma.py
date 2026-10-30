"""Firma electrónica de la póliza (HU21). Móvil y web usan estos mismos endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion import repositorio as repo
from app.suscripcion.api import _token
from app.suscripcion.firma import DocumentoParaFirmar, OrigenFirma, ServicioFirma
from app.suscripcion.modelos_db import FirmaDB

router = APIRouter(prefix="/v1/suscripciones", tags=["firma"])


def obtener_servicio_firma(request: Request) -> ServicioFirma:
    return request.app.state.servicio_firma


Firma = Annotated[ServicioFirma, Depends(obtener_servicio_firma)]


class DocumentoFirmaOut(BaseModel):
    """Lo que el cliente necesita para revisar y firmar. El PDF se descarga aparte, en `documento-firma/pdf`."""

    suscripcion_id: str
    numero_poliza: str
    documento_sha256: str = Field(description="Hash del PDF; el cliente lo devuelve al firmar para probar qué firmó")
    estado: Literal["PENDIENTE", "FIRMADA"]
    vence_en: datetime | None
    autenticacion: str = Field(description="Cómo se autentica el firmante (OTP)")


class FirmaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documento_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    documento_revisado: bool
    codigo: str = Field(min_length=1, max_length=12, description="Código de un solo uso (OTP)")
    canal: Literal["MOVIL", "WEB"]
    dispositivo: str | None = Field(default=None, max_length=200)


class FirmaOut(BaseModel):
    suscripcion_id: str
    poliza_id: str
    estado_saga: str
    documento_sha256: str
    firmada_en: datetime
    canal: str
    metodo_autenticacion: str


def _salida(firma: FirmaDB, estado_saga: str) -> FirmaOut:
    return FirmaOut(
        suscripcion_id=firma.suscripcion_id,
        poliza_id=firma.poliza_id,
        estado_saga=estado_saga,
        documento_sha256=firma.documento_sha256,
        firmada_en=repo.utc(firma.firmada_en),
        canal=firma.canal,
        metodo_autenticacion=firma.metodo_autenticacion,
    )


def _salida_documento(d: DocumentoParaFirmar) -> DocumentoFirmaOut:
    return DocumentoFirmaOut(
        suscripcion_id=d.suscripcion_id,
        numero_poliza=d.numero_poliza,
        documento_sha256=d.documento_sha256,
        estado=d.estado,  # type: ignore[arg-type]
        vence_en=d.vence_en,
        autenticacion=d.metodo_autenticacion,
    )


def _ip(request: Request) -> str | None:
    """La IP del cliente: la primera de `X-Forwarded-For` (detrás de CloudFront y el API Gateway) o la del socket."""
    reenviada = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return reenviada or (request.client.host if request.client else None)


@router.get("/{suscripcion_id}/documento-firma", response_model=DocumentoFirmaOut, summary="Documento por firmar (hash y vencimiento)")
async def documento_firma(
    suscripcion_id: str, servicio: Firma, authorization: Annotated[str | None, Header()] = None
) -> DocumentoFirmaOut:
    return _salida_documento(await servicio.documento_para_firmar(suscripcion_id, _token(authorization)))


@router.get(
    "/{suscripcion_id}/documento-firma/pdf",
    summary="PDF de la póliza por firmar",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def documento_firma_pdf(
    suscripcion_id: str, servicio: Firma, authorization: Annotated[str | None, Header()] = None
) -> Response:
    contenido = await servicio.contenido_documento(suscripcion_id, _token(authorization))
    return Response(content=contenido, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post("/{suscripcion_id}/firma/codigo", status_code=202, summary="Envía el código de un solo uso al firmante")
async def solicitar_codigo(
    suscripcion_id: str, servicio: Firma, authorization: Annotated[str | None, Header()] = None
) -> Response:
    await servicio.solicitar_codigo(suscripcion_id, _token(authorization))
    return Response(status_code=202)


@router.post(
    "/{suscripcion_id}/firma",
    response_model=FirmaOut,
    summary="Firma la póliza; repetirla devuelve la misma firma",
)
async def firmar(
    suscripcion_id: str,
    cuerpo: FirmaIn,
    request: Request,
    response: Response,
    servicio: Firma,
    authorization: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> FirmaOut:
    firma, creada = await servicio.firmar(
        suscripcion_id,
        _token(authorization),
        documento_sha256=cuerpo.documento_sha256,
        documento_revisado=cuerpo.documento_revisado,
        codigo=cuerpo.codigo,
        origen=OrigenFirma(canal=cuerpo.canal, ip=_ip(request), dispositivo=cuerpo.dispositivo or user_agent),
    )
    if not creada:
        response.headers["Idempotent-Replay"] = "true"
    estado = await servicio.estado_saga(suscripcion_id)
    return _salida(firma, estado)


@router.get("/{suscripcion_id}/firma", response_model=FirmaOut, summary="Evidencia de la firma")
async def consultar_firma(
    suscripcion_id: str, servicio: Firma, authorization: Annotated[str | None, Header()] = None
) -> FirmaOut:
    firma = await servicio.firma_de(suscripcion_id, _token(authorization))
    return _salida(firma, await servicio.estado_saga(suscripcion_id))


@router.get(
    "/{suscripcion_id}/firma/documento",
    summary="Descarga la póliza firmada (PDF)",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def descargar_firmada(
    suscripcion_id: str, servicio: Firma, authorization: Annotated[str | None, Header()] = None
) -> Response:
    contenido, numero = await servicio.copia_firmada(suscripcion_id, _token(authorization))
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="poliza-{numero}-firmada.pdf"', "Cache-Control": "no-store"},
    )
