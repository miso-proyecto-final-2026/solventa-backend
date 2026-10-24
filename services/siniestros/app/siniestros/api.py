"""API de Siniestros (HU27). Acceso por llave de API del proveedor en `X-Service-Key`."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel

from app.seguridad import clave_valida
from app.siniestros.dominio import TIPOS_EVENTO
from app.siniestros.errores import EventoInvalido, NoAutenticado
from app.siniestros.servicio import Receptor


def exigir_proveedor(
    request: Request, x_service_key: Annotated[str | None, Header()] = None
) -> str:
    """Devuelve la fuente que representa la llave. 401 sin llave o con una inválida (criterio 4)."""
    if not x_service_key:
        raise NoAutenticado
    claves: dict[str, str] = request.app.state.api_keys
    if not clave_valida(x_service_key, claves):
        raise NoAutenticado
    return next(fuente for clave, fuente in claves.items() if clave_valida(x_service_key, [clave]))


Proveedor = Annotated[str, Depends(exigir_proveedor)]


def obtener_receptor(request: Request) -> Receptor:
    return request.app.state.receptor


ReceptorActual = Annotated[Receptor, Depends(obtener_receptor)]


class EventoIn(BaseModel):
    """Solo documenta el contrato: la validación (con registro del rechazo) la hace el servicio."""

    evento_id: str
    tipo: str
    fuente: str
    ocurrido_en: str
    datos: dict[str, Any]
    poliza_id: str | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "evento_id": "evt-2026-10-23-0001",
                    "tipo": "sismo",
                    "fuente": "proveedor-sismos",
                    "ocurrido_en": "2026-10-23T14:05:00Z",
                    "datos": {"magnitud": 6.4, "latitud": 4.71, "longitud": -74.07},
                }
            ],
            "description": "Tipos aceptados: " + ", ".join(sorted(TIPOS_EVENTO)),
        }
    }


class AcuseOut(BaseModel):
    evento_uuid: str
    evento_id: str
    estado: str
    duplicado: bool
    recibido_en: str


class ResumenOut(BaseModel):
    recibidos: int
    rechazados: int
    publicados: int
    procesados: int
    reentregas: int


router = APIRouter(prefix="/v1/siniestros", tags=["siniestros"])


@router.post(
    "/eventos",
    response_model=AcuseOut,
    status_code=202,
    summary="Recibir un evento paramétrico de vuelos o clima",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": EventoIn.model_json_schema()}},
        }
    },
)
async def recibir_evento(
    request: Request, response: Response, fuente: Proveedor, receptor: ReceptorActual
) -> AcuseOut:
    # Se lee el cuerpo a mano: un JSON malformado o incompleto debe ser 400 con registro del
    # rechazo, no el 422 que FastAPI genera antes de que el servicio lo vea.
    try:
        cuerpo: Any = json.loads(await request.body())
    except ValueError:
        cuerpo = None
    if cuerpo is None:
        cuerpo_invalido = EventoInvalido([{"campo": "cuerpo", "motivo": "debe ser un objeto JSON"}])
        await receptor.registrar_cuerpo_invalido(fuente, cuerpo_invalido.detalles)
        raise cuerpo_invalido
    acuse = await receptor.recibir(cuerpo, fuente)
    if acuse.duplicado:
        response.status_code = 200
    return AcuseOut(
        evento_uuid=acuse.evento_uuid,
        evento_id=acuse.evento_id,
        estado=acuse.estado,
        duplicado=acuse.duplicado,
        recibido_en=acuse.recibido_en.isoformat(),
    )


@router.get(
    "/eventos/resumen",
    response_model=ResumenOut,
    summary="Conteos de eventos recibidos, publicados y procesados (verificación de ráfagas)",
)
async def resumen(_fuente: Proveedor, receptor: ReceptorActual) -> dict[str, int]:
    return await receptor.resumen()
