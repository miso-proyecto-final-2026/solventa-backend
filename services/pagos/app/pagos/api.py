"""API interna de Pagos (HU18). Sin borde público: acceso por clave de servicio en `X-Service-Key`.

Ningún endpoint recibe ni devuelve el número de tarjeta. El token de la pasarela entra, pero nunca sale.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.pagos.dominio import parece_pan
from app.pagos.errores import (
    DatoDeTarjetaNoPermitido,
    NoAutenticado,
    SinPermiso,
    TransaccionNoEncontrada,
)
from app.pagos.modelos_db import TransaccionDB
from app.pagos.servicio import ComandoCobro, ServicioCobros, clave_por_defecto
from app.seguridad import clave_valida

router = APIRouter(prefix="/v1/pagos", tags=["pagos"])


def exigir_servicio(request: Request, x_service_key: Annotated[str | None, Header()] = None) -> str:
    """Devuelve el nombre del llamador. 401 sin cabecera, 403 con una clave desconocida; sin claves nadie accede."""
    if not x_service_key:
        raise NoAutenticado
    claves: dict[str, str] = request.app.state.api_keys
    if not clave_valida(x_service_key, claves):
        raise SinPermiso
    return next(nombre for clave, nombre in claves.items() if clave_valida(x_service_key, [clave]))


Llamador = Annotated[str, Depends(exigir_servicio)]


def obtener_servicio(request: Request) -> ServicioCobros:
    return request.app.state.servicio_cobros


Servicio = Annotated[ServicioCobros, Depends(obtener_servicio)]


def _sin_pan(valor: str) -> str:
    if parece_pan(valor):
        raise DatoDeTarjetaNoPermitido
    return valor


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # un campo `pan` o `numero` se rechaza en vez de ignorarse

    token: str = Field(min_length=1, max_length=200, description="Token emitido por la pasarela; nunca el número de tarjeta")
    cliente_id: str | None = Field(default=None, max_length=64)

    @field_validator("token")
    @classmethod
    def _token_sin_pan(cls, valor: str) -> str:
        return _sin_pan(valor)


class TokenOut(BaseModel):
    token_id: int
    ultimos4: str
    marca: str


class CobroIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suscripcion_id: str = Field(min_length=1, max_length=36)
    cliente_id: str = Field(min_length=1, max_length=64)
    metodo_pago: str = Field(min_length=1, max_length=200, description="Token de la pasarela; nunca el número de tarjeta")
    monto: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    moneda: Literal["COP"] = "COP"
    periodicidad_pago: Literal["ANUAL", "MENSUAL"] | None = None

    @field_validator("metodo_pago")
    @classmethod
    def _token_sin_pan(cls, valor: str) -> str:
        return _sin_pan(valor)


class RechazoOut(BaseModel):
    codigo: str | None
    motivo: str | None


class TransaccionOut(BaseModel):
    id: str
    tipo: str
    estado: str
    suscripcion_id: str
    monto: Decimal
    moneda: str
    periodicidad_pago: str | None
    referencia_pasarela: str | None
    ultimos4: str | None
    marca: str | None
    rechazo: RechazoOut | None
    gracia_hasta: datetime | None
    intentos: int
    creada_en: datetime
    resuelta_en: datetime | None


def a_salida(tx: TransaccionDB, ultimos4: str | None, marca: str | None) -> TransaccionOut:
    rechazo = RechazoOut(codigo=tx.codigo_rechazo, motivo=tx.motivo_rechazo) if tx.estado == "RECHAZADA" else None
    return TransaccionOut(
        id=tx.id,
        tipo=tx.tipo,
        estado=tx.estado,
        suscripcion_id=tx.suscripcion_id,
        monto=Decimal(tx.monto).quantize(Decimal("0.01")),
        moneda=tx.moneda,
        periodicidad_pago=tx.periodicidad_pago,
        referencia_pasarela=tx.referencia_pasarela,
        ultimos4=ultimos4,
        marca=marca,
        rechazo=rechazo,
        gracia_hasta=tx.gracia_hasta,
        intentos=tx.intentos,
        creada_en=tx.created_at,
        resuelta_en=tx.resuelta_en,
    )


async def _salida(servicio: ServicioCobros, transaccion_id: str) -> TransaccionOut:
    encontrada = await asyncio.to_thread(servicio.consultar, transaccion_id)
    if encontrada is None:
        raise TransaccionNoEncontrada
    return a_salida(*encontrada)


@router.post(
    "/tokenizar",
    status_code=201,
    response_model=TokenOut,
    summary="Registra un token de la pasarela (nunca el PAN) con su marca y últimos 4 dígitos",
)
async def tokenizar(cuerpo: TokenIn, servicio: Servicio, _llamador: Llamador) -> TokenOut:
    token_id, ultimos4, marca = await servicio.tokenizar(cuerpo.token, cuerpo.cliente_id)
    return TokenOut(token_id=token_id, ultimos4=ultimos4, marca=marca)


@router.post(
    "/cobros",
    status_code=201,
    response_model=TransaccionOut,
    summary="Cobra la prima (entrada interna; la saga usa normalmente el evento cobro_prima_solicitado)",
    responses={
        200: {"description": "Repetición de la misma clave: el resultado del primer cobro"},
        202: {"description": "Sin respuesta cierta de la pasarela todavía: queda PENDIENTE y se concilia"},
    },
)
async def cobrar(
    cuerpo: CobroIn,
    response: Response,
    servicio: Servicio,
    _llamador: Llamador,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> TransaccionOut:
    comando = ComandoCobro.validado(
        idempotency_key=idempotency_key or clave_por_defecto(cuerpo.suscripcion_id),
        suscripcion_id=cuerpo.suscripcion_id,
        cliente_id=cuerpo.cliente_id,
        token=cuerpo.metodo_pago,
        monto=cuerpo.monto,
        moneda=cuerpo.moneda,
        periodicidad_pago=cuerpo.periodicidad_pago,
    )
    tx, creada = await servicio.cobrar(comando)
    salida = await _salida(servicio, tx.id)
    if not creada:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    elif salida.estado == "PENDIENTE":
        response.status_code = 202
    return salida


@router.get("/transacciones/{transaccion_id}", response_model=TransaccionOut, summary="Estado de una transacción")
async def consultar(transaccion_id: str, servicio: Servicio, _llamador: Llamador) -> TransaccionOut:
    return await _salida(servicio, transaccion_id)
