"""Firma por enlace (HU39): rutas públicas del cliente y la ruta interna con la que Notificaciones pide el token.

El token viaja en una cabecera, no en la URL: así no queda en los logs de CloudFront ni del API Gateway. La web lo recibe en
el fragmento de la URL del correo, que el navegador no envía al servidor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion.api_certificado import exigir_servicio
from app.suscripcion.api_firma import (
    DocumentoFirmaOut,
    FirmaOut,
    _ip,
    _salida,
    _salida_documento,
)
from app.suscripcion.enlace_firma import ServicioEnlaceFirma
from app.suscripcion.firma import OrigenFirma

router = APIRouter(tags=["firma-por-enlace"])
PREFIJO = "/v1/suscripciones/enlace-firma"


def obtener_servicio_enlace(request: Request) -> ServicioEnlaceFirma:
    return request.app.state.servicio_enlace_firma


Enlaces = Annotated[ServicioEnlaceFirma, Depends(obtener_servicio_enlace)]
TokenEnlace = Annotated[str | None, Header(alias="X-Enlace-Firma", description="Token del enlace recibido por correo")]


class FirmaEnlaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documento_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    documento_revisado: bool
    codigo: str = Field(min_length=1, max_length=12, description="Código de un solo uso del cliente (OTP)")


@router.get(f"{PREFIJO}/documento", response_model=DocumentoFirmaOut, summary="Documento por firmar con el enlace (hash y estado)")
async def documento(servicio: Enlaces, token: TokenEnlace = None) -> DocumentoFirmaOut:
    return _salida_documento(await servicio.documento(token))


@router.get(
    f"{PREFIJO}/documento/pdf",
    summary="PDF por firmar con el enlace",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def documento_pdf(servicio: Enlaces, token: TokenEnlace = None) -> Response:
    return Response(content=await servicio.pdf(token), media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post(f"{PREFIJO}/codigo", status_code=202, summary="Envía el código de un solo uso al cliente")
async def solicitar_codigo(servicio: Enlaces, token: TokenEnlace = None) -> Response:
    await servicio.solicitar_codigo(token)
    return Response(status_code=202)


@router.post(f"{PREFIJO}/firma", response_model=FirmaOut, summary="Firma con el enlace y el código; repetirla devuelve la misma firma")
async def firmar(
    cuerpo: FirmaEnlaceIn,
    request: Request,
    response: Response,
    servicio: Enlaces,
    token: TokenEnlace = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> FirmaOut:
    firma, creada, suscripcion_id = await servicio.firmar(
        token,
        documento_sha256=cuerpo.documento_sha256,
        documento_revisado=cuerpo.documento_revisado,
        codigo=cuerpo.codigo,
        origen=OrigenFirma(canal="WEB", ip=_ip(request), dispositivo=user_agent),
    )
    if not creada:
        response.headers["Idempotent-Replay"] = "true"
    return _salida(firma, await servicio.estado_saga(suscripcion_id))


class TokenEnlaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idioma: Literal["es-CO", "en-US"] = "es-CO"


class TokenEnlaceOut(BaseModel):
    ruta: str = Field(description="Ruta de la página de firma; el servicio que la envía le antepone la URL base del borde")
    vence_en: datetime


@router.post(
    "/v1/interno/firma/enlaces/{enlace_id}/token",
    response_model=TokenEnlaceOut,
    summary="Token de un enlace activo, para el correo (interno: clave de servicio)",
    dependencies=[Depends(exigir_servicio)],
)
async def token_enlace(enlace_id: str, cuerpo: TokenEnlaceIn, servicio: Enlaces) -> TokenEnlaceOut:
    token, vence = await servicio.token(enlace_id)
    return TokenEnlaceOut(ruta=f"/{cuerpo.idioma}/sign#t={token}", vence_en=vence)
