"""API de Suscripción (HU15). El cliente se identifica con su token; la identidad la valida MS Identidad."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.suscripcion.dominio import PASOS
from app.suscripcion.modelos_db import SuscripcionDB
from app.suscripcion.servicio import ServicioSuscripcion

log = logging.getLogger("suscripcion")
router = APIRouter(prefix="/v1/suscripciones", tags=["suscripciones"])


def obtener_servicio(request: Request) -> ServicioSuscripcion:
    return request.app.state.servicio_suscripcion


Servicio = Annotated[ServicioSuscripcion, Depends(obtener_servicio)]


def _token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


class SuscripcionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cotizacion_id: str = Field(min_length=1, max_length=36)
    metodo_pago: str = Field(min_length=1, max_length=200, description="Token de la pasarela; nunca el número de tarjeta")
    acepta_terminos: bool
    periodicidad_pago: Literal["ANUAL", "MENSUAL"] = Field(
        default="ANUAL", description="Cómo paga el cliente; se cobra el primer período (HU18)"
    )


class DecisionOut(BaseModel):
    decision: str
    motivos: list[str]
    ajuste: dict[str, Any] | None
    reglas_version: str | None
    decidido_en: datetime | None
    duracion_decision_ms: int | None


class CobroOut(BaseModel):
    """Resultado del paso COBRO según MS Pagos. Nunca incluye el token ni datos de la tarjeta."""

    estado: str
    transaccion_id: str | None
    referencia_pasarela: str | None
    codigo: str | None
    motivo: str | None
    gracia_hasta: datetime | None
    resuelto_en: datetime | None


class SuscripcionOut(BaseModel):
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
    revision_asistida: bool
    creada_en: datetime


def a_salida(s: SuscripcionDB, revision: bool) -> SuscripcionOut:
    decision = None
    if s.decision is not None:
        decision = DecisionOut(
            decision=s.decision,
            motivos=list(s.motivos or []),
            ajuste=s.ajuste,
            reglas_version=s.reglas_version,
            decidido_en=s.decidido_en,
            duracion_decision_ms=s.duracion_decision_ms,
        )
    cobro = None
    if s.cobro_estado is not None:
        cobro = CobroOut(
            estado=s.cobro_estado,
            transaccion_id=s.cobro_transaccion_id,
            referencia_pasarela=s.cobro_referencia,
            codigo=s.cobro_codigo,
            motivo=s.cobro_motivo,
            gracia_hasta=s.cobro_gracia_hasta,
            resuelto_en=s.cobro_resuelto_en,
        )
    return SuscripcionOut(
        id=s.id,
        cotizacion_id=s.cotizacion_id,
        estado_saga=s.estado_saga,
        paso_actual=s.paso_actual,
        pasos=PASOS,
        producto=s.producto,
        moneda=s.moneda,
        monto_asegurado=int(s.monto_asegurado),
        periodicidad_pago=s.periodicidad_pago,
        decision=decision,
        cobro=cobro,
        revision_asistida=revision,
        creada_en=s.created_at,
    )


def _decidir_en_segundo_plano(request: Request, suscripcion_id: str) -> None:
    async def tarea() -> None:
        try:
            await request.app.state.servicio_suscripcion.decidir(suscripcion_id)
        except Exception:
            log.exception("no se pudo decidir suscripcion_id=%s", suscripcion_id)

    # Se guarda la referencia para que el recolector no la elimine a mitad de camino.
    t = asyncio.create_task(tarea())
    request.app.state.tareas.add(t)
    t.add_done_callback(request.app.state.tareas.discard)


@router.post(
    "",
    status_code=202,
    response_model=SuscripcionOut,
    summary="Inicia la saga de suscripción; la decisión se toma enseguida y se consulta con GET",
)
async def iniciar(
    cuerpo: SuscripcionIn,
    request: Request,
    response: Response,
    servicio: Servicio,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> SuscripcionOut:
    susc, creada = await servicio.iniciar(
        token=_token(authorization),
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


@router.get("/{suscripcion_id}", response_model=SuscripcionOut, summary="Estado de la saga")
async def consultar(
    suscripcion_id: str,
    servicio: Servicio,
    authorization: Annotated[str | None, Header()] = None,
) -> SuscripcionOut:
    susc = await servicio.consultar(suscripcion_id, _token(authorization))
    revisiones = await asyncio.to_thread(servicio.revisiones, suscripcion_id)
    return a_salida(susc, revision=bool(revisiones))
