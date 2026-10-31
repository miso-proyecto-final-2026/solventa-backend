"""API de Notificaciones (HU22): registro del dispositivo, bandeja del push y envíos fallidos para el operador."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.notificaciones import repositorio as repo
from app.notificaciones.acceso import ClienteActual, OperadorActual
from app.notificaciones.errores import NotificacionNoEncontrada
from app.notificaciones.modelos_db import BandejaPushDB

router = APIRouter(tags=["notificaciones"])


def _fabrica(request: Request):
    return request.app.state.fabrica


class DispositivoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispositivo_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$", description="Lo genera la app; en un push real sería el token de FCM")
    idioma: Literal["es-CO", "en-US"] = Field(description="El idioma del teléfono: el de los textos que recibirá el cliente")


class DispositivoOut(BaseModel):
    dispositivo_id: str
    idioma: str


class NotificacionOut(BaseModel):
    id: str
    tipo: str
    titulo: str
    texto: str
    titulo_publico: str = Field(description="Lo que se ve con la pantalla de bloqueo: sin datos de la póliza")
    texto_publico: str
    prioridad: Literal["ALTA", "NORMAL"]
    idioma: str
    suscripcion_id: str
    creada_en: datetime
    recibida: bool


class EnvioFallidoOut(BaseModel):
    id: str
    canal: str
    tipo: str
    cliente_id: str
    intentos: int
    ultimo_error: str | None
    creado_en: datetime


def _ahora(request: Request) -> datetime:
    """La hora de la API; las pruebas la sustituyen por el reloj del servicio para ordenar dispositivo y notificación."""
    reloj = getattr(request.app.state, "reloj", None)
    return reloj() if reloj is not None else datetime.now(UTC)


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


def _notificacion(b: BandejaPushDB) -> NotificacionOut:
    return NotificacionOut(
        id=b.envio_id,
        tipo=b.tipo,
        titulo=b.titulo,
        texto=b.texto,
        titulo_publico=b.titulo_publico,
        texto_publico=b.texto_publico,
        prioridad=b.prioridad,  # type: ignore[arg-type]
        idioma=b.idioma,
        suscripcion_id=b.suscripcion_id,
        creada_en=_utc(b.creado_en),
        recibida=b.recibida_en is not None,
    )


@router.post("/v1/dispositivos", response_model=DispositivoOut, summary="Registra el dispositivo del cliente y su idioma")
async def registrar_dispositivo(cuerpo: DispositivoIn, request: Request, cliente_id: ClienteActual) -> DispositivoOut:
    """Idempotente: registrar otra vez el mismo dispositivo solo actualiza el idioma."""

    def guardar() -> None:
        with _fabrica(request)() as s, s.begin():
            repo.registrar_dispositivo(s, cliente_id, cuerpo.dispositivo_id, cuerpo.idioma, _ahora(request))

    await asyncio.to_thread(guardar)
    return DispositivoOut(dispositivo_id=cuerpo.dispositivo_id, idioma=cuerpo.idioma)


@router.get("/v1/notificaciones", response_model=list[NotificacionOut], summary="Notificaciones push del cliente (bandeja)")
async def listar_notificaciones(
    request: Request,
    cliente_id: ClienteActual,
    solo_nuevas: Annotated[bool, Query(description="Solo las que la app aún no confirmó y se crearon desde que el cliente registró su dispositivo")] = True,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[NotificacionOut]:
    def leer() -> list[NotificacionOut]:
        with _fabrica(request)() as s:
            return [_notificacion(b) for b in repo.bandeja_de(s, cliente_id, solo_nuevas=solo_nuevas, limite=limite)]

    return await asyncio.to_thread(leer)


@router.post("/v1/notificaciones/{notificacion_id}/recibida", status_code=204, summary="La app confirma que ya mostró la notificación")
async def marcar_recibida(notificacion_id: str, request: Request, cliente_id: ClienteActual) -> Response:
    """Confirmarla otra vez no cambia nada. Una notificación ajena responde como inexistente."""

    def marcar() -> bool:
        with _fabrica(request)() as s, s.begin():
            return repo.marcar_recibida(s, cliente_id, notificacion_id, _ahora(request))

    if not await asyncio.to_thread(marcar):
        raise NotificacionNoEncontrada
    return Response(status_code=204)


@router.get(
    "/v1/notificaciones/envios-fallidos",
    response_model=list[EnvioFallidoOut],
    summary="Envíos que agotaron sus reintentos (operador)",
)
async def envios_fallidos(request: Request, _operador: OperadorActual, limite: Annotated[int, Query(ge=1, le=200)] = 50) -> list[EnvioFallidoOut]:
    def leer() -> list[EnvioFallidoOut]:
        with _fabrica(request)() as s:
            return [
                EnvioFallidoOut(
                    id=e.id, canal=e.canal, tipo=e.tipo, cliente_id=e.cliente_id, intentos=e.intentos,
                    ultimo_error=e.ultimo_error, creado_en=_utc(e.creado_en),
                )
                for e in repo.envios_fallidos(s, limite)
            ]

    return await asyncio.to_thread(leer)

