"""Certificado de cobertura (HU22): descarga del titular, enlace para el correo y descarga por enlace."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion.api import _token
from app.suscripcion.certificado import (
    Certificado,
    ServicioCertificado,
    normalizar_idioma,
)
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.errores import ClaveDeServicioInvalida

router = APIRouter(tags=["certificado"])


def obtener_servicio_certificado(request: Request) -> ServicioCertificado:
    return request.app.state.servicio_certificado


Certificados = Annotated[ServicioCertificado, Depends(obtener_servicio_certificado)]


def exigir_servicio(x_service_key: Annotated[str | None, Header()] = None) -> None:
    """Las rutas internas exigen la clave de otro servicio (`SUSCRIPCION_SERVICE_KEYS`). Sin claves configuradas, nadie entra."""
    claves = ConfigSuscripcion.desde_entorno().claves_de_servicio
    if not x_service_key or not any(hmac.compare_digest(x_service_key, c) for c in claves):
        raise ClaveDeServicioInvalida


def _pdf(certificado: Certificado) -> Response:
    return Response(
        content=certificado.contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="certificado-{certificado.numero_poliza}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/v1/suscripciones/{suscripcion_id}/certificado",
    summary="Certificado de cobertura en PDF (solo el titular)",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def descargar_certificado(
    suscripcion_id: str,
    servicio: Certificados,
    authorization: Annotated[str | None, Header()] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> Response:
    """El idioma sale de `Accept-Language` (es-CO por defecto). Una póliza ajena responde como inexistente."""
    return _pdf(await servicio.descargar_titular(suscripcion_id, _token(authorization), normalizar_idioma(accept_language)))


class EnlaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poliza_id: str = Field(min_length=1, max_length=36)
    idioma: Literal["es-CO", "en-US"] = "es-CO"


class EnlaceOut(BaseModel):
    ruta: str = Field(description="Ruta pública del enlace; el servicio que lo envía le antepone la URL base del borde")
    vence_en: datetime


@router.post(
    "/v1/interno/certificados/enlaces",
    response_model=EnlaceOut,
    status_code=201,
    summary="Emite un enlace de descarga con vencimiento (interno: clave de servicio)",
    dependencies=[Depends(exigir_servicio)],
)
async def emitir_enlace(cuerpo: EnlaceIn, servicio: Certificados) -> EnlaceOut:
    token, vence = await servicio.emitir_enlace(cuerpo.poliza_id, cuerpo.idioma)
    return EnlaceOut(ruta=f"/v1/certificados/{token}", vence_en=vence)


@router.get(
    "/v1/certificados/{token}",
    summary="Descarga el certificado con un enlace firmado y vigente",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}, 401: {}, 410: {}},
)
async def descargar_por_enlace(token: str, servicio: Certificados) -> Response:
    return _pdf(await servicio.descargar_por_enlace(token))

