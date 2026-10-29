"""API de gestión de cobros fallidos para la consola web (HU19).

Es el único prefijo de Pagos que se publica en el borde (`/v1/pagos/cobros-fallidos`, enmienda al ADR-0014) y exige el JWT
de un asesor u operador (ADR-0008). Nunca devuelve el token de la tarjeta, el número de tarjeta ni datos de contacto del
cliente: de la tarjeta, solo marca y últimos 4.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.pagos.acceso import GestorActual, exigir_gestor
from app.pagos.dominio import EstadoTransaccion
from app.pagos.gestion import (
    CasoVista,
    DetalleCaso,
    ResultadoReintento,
    ServicioGestion,
)

TAMANO_MAXIMO = 100


class ErrorDetalleOut(BaseModel):
    campo: str
    motivo: str


class ErrorCuerpoOut(BaseModel):
    code: str
    message: str
    details: list[ErrorDetalleOut]
    trace_id: str


class ErrorOut(BaseModel):
    error: ErrorCuerpoOut


_ERRORES = {
    401: {"model": ErrorOut, "description": "Falta el token, no es válido o venció (sesión vencida)."},
    403: {"model": ErrorOut, "description": "El token no es de un asesor ni de un operador."},
}

router = APIRouter(
    prefix="/v1/pagos/cobros-fallidos",
    tags=["cobros-fallidos"],
    dependencies=[Depends(exigir_gestor)],
    responses=_ERRORES,
)


def obtener_gestion(request: Request) -> ServicioGestion:
    return request.app.state.servicio_gestion


Gestion = Annotated[ServicioGestion, Depends(obtener_gestion)]

EstadoCasoTexto = Literal["PENDIENTE", "VENCIDO", "RECUPERADO"]
EstadoGracia = Literal["VIGENTE", "POR_VENCER", "VENCIDA"]


class TarjetaOut(BaseModel):
    marca: str | None
    ultimos4: str | None


class CasoOut(BaseModel):
    id: str
    suscripcion_id: str
    cliente_id: str
    estado: EstadoCasoTexto
    monto: Decimal
    moneda: str
    intentos: int = Field(description="Cobros hechos: el original más los reintentos")
    ultimo_intento_en: datetime
    codigo_rechazo: str | None
    motivo_rechazo: str | None
    gracia_hasta: datetime
    estado_gracia: EstadoGracia | None = Field(description="None si el cobro ya se recuperó")
    tarjeta: TarjetaOut


class PaginaCasosOut(BaseModel):
    items: list[CasoOut]
    total: int
    pagina: int
    tamano: int


class IntentoOut(BaseModel):
    numero: int
    resultado: str
    codigo: str | None
    motivo: str | None
    referencia_pasarela: str | None
    fecha: datetime


class CobroOut(BaseModel):
    transaccion_id: str
    es_reintento: bool
    estado: str
    monto: Decimal
    creado_en: datetime
    resuelto_en: datetime | None
    intentos: list[IntentoOut]


class NotaOut(BaseModel):
    id: int
    autor_id: str
    autor_nombre: str
    autor_rol: str
    texto: str
    creada_en: datetime


class CasoDetalleOut(CasoOut):
    reintentos: int
    operador_notificado_en: datetime | None
    resuelto_en: datetime | None
    cobros: list[CobroOut]
    notas: list[NotaOut]


class NotaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: str = Field(min_length=1, max_length=1000)


class ReintentoOut(BaseModel):
    caso_id: str
    transaccion_id: str
    numero_reintento: int
    resultado: Literal["APROBADO", "RECHAZADO", "EN_PROCESO"]
    codigo_rechazo: str | None
    motivo_rechazo: str | None
    detalle_pasarela: str | None = Field(
        description="Con EN_PROCESO: NO_DISPONIBLE (la pasarela no recibió la orden) o SIN_RESPUESTA (se concilia)"
    )
    estado_caso: EstadoCasoTexto
    repeticion: bool = Field(description="True si no se cobró otra vez: dos clics o un reintento ya en curso")


def _caso(vista: CasoVista) -> dict:
    c = vista.caso
    return {
        "id": c.id,
        "suscripcion_id": c.suscripcion_id,
        "cliente_id": c.cliente_id,
        "estado": c.estado,
        "monto": Decimal(c.monto).quantize(Decimal("0.01")),
        "moneda": c.moneda,
        "intentos": 1 + c.reintentos,
        "ultimo_intento_en": c.ultimo_intento_en,
        "codigo_rechazo": c.codigo_rechazo,
        "motivo_rechazo": c.motivo_rechazo,
        "gracia_hasta": c.gracia_hasta,
        "estado_gracia": vista.estado_gracia,
        "tarjeta": TarjetaOut(
            marca=vista.token.marca if vista.token else None, ultimos4=vista.token.ultimos4 if vista.token else None
        ),
    }


def _detalle(d: DetalleCaso) -> CasoDetalleOut:
    c = d.vista.caso
    cobros = [
        CobroOut(
            transaccion_id=tx.id,
            es_reintento=tx.id != c.id,
            estado=tx.estado,
            monto=Decimal(tx.monto).quantize(Decimal("0.01")),
            creado_en=tx.created_at,
            resuelto_en=tx.resuelta_en,
            intentos=[
                IntentoOut(
                    numero=i.numero, resultado=i.resultado, codigo=i.codigo, motivo=i.motivo,
                    referencia_pasarela=i.referencia_pasarela, fecha=i.created_at,
                )
                for i in intentos
            ],
        )
        for tx, intentos in d.transacciones
    ]
    notas = [_nota(n) for n in d.notas]
    return CasoDetalleOut(
        **_caso(d.vista),
        reintentos=c.reintentos,
        operador_notificado_en=c.operador_notificado_en,
        resuelto_en=c.resuelto_en,
        cobros=cobros,
        notas=notas,
    )


def _nota(n) -> NotaOut:
    return NotaOut(
        id=n.id, autor_id=n.autor_id, autor_nombre=n.autor_nombre, autor_rol=n.autor_rol, texto=n.texto, creada_en=n.created_at
    )


def _reintento(r: ResultadoReintento) -> ReintentoOut:
    estado = r.transaccion.estado
    resultado = {EstadoTransaccion.APROBADA.value: "APROBADO", EstadoTransaccion.RECHAZADA.value: "RECHAZADO"}.get(
        estado, "EN_PROCESO"
    )
    rechazado = resultado == "RECHAZADO"
    return ReintentoOut(
        caso_id=r.caso.id,
        transaccion_id=r.transaccion.id,
        numero_reintento=r.numero,
        resultado=resultado,
        codigo_rechazo=r.transaccion.codigo_rechazo if rechazado else None,
        motivo_rechazo=r.transaccion.motivo_rechazo if rechazado else None,
        detalle_pasarela=r.ultimo_intento if resultado == "EN_PROCESO" else None,
        estado_caso=r.caso.estado,
        repeticion=not r.creado,
    )


@router.get("", response_model=PaginaCasosOut, summary="Bandeja de cobros fallidos (los más urgentes primero)")
async def bandeja(
    gestion: Gestion,
    estado: Annotated[list[EstadoCasoTexto] | None, Query(description="Por defecto, PENDIENTE y VENCIDO")] = None,
    por_vencer: Annotated[bool, Query(description="Solo los que vencen en menos de 24 h (o ya vencidos)")] = False,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=TAMANO_MAXIMO)] = 20,
) -> PaginaCasosOut:
    resultado = await asyncio.to_thread(
        gestion.bandeja, estados=estado, solo_por_vencer=por_vencer, pagina=pagina, tamano=tamano
    )
    return PaginaCasosOut(
        items=[CasoOut(**_caso(v)) for v in resultado.casos],
        total=resultado.total,
        pagina=resultado.pagina,
        tamano=resultado.tamano,
    )


@router.get(
    "/{caso_id}",
    response_model=CasoDetalleOut,
    summary="Detalle: historial de cobros e intentos, motivo, código de la pasarela y notas",
    responses={404: {"model": ErrorOut, "description": "El caso no existe"}},
)
async def detalle(caso_id: str, gestion: Gestion) -> CasoDetalleOut:
    return _detalle(await asyncio.to_thread(gestion.detalle, caso_id))


@router.post(
    "/{caso_id}/notas",
    status_code=201,
    response_model=NotaOut,
    summary="Registra una nota de seguimiento con autor y fecha",
    responses={
        404: {"model": ErrorOut, "description": "El caso no existe"},
        422: {"model": ErrorOut, "description": "Texto vacío, demasiado largo o con un número de tarjeta"},
    },
)
async def agregar_nota(caso_id: str, cuerpo: NotaIn, gestion: Gestion, gestor: GestorActual) -> NotaOut:
    return _nota(await asyncio.to_thread(gestion.agregar_nota, caso_id, cuerpo.texto, gestor))


@router.post(
    "/{caso_id}/reintentar",
    response_model=ReintentoOut,
    summary="Reintenta el cobro con el método de pago registrado del cliente (idempotente)",
    responses={
        202: {"model": ReintentoOut, "description": "La pasarela aún no da una respuesta cierta: queda EN_PROCESO"},
        404: {"model": ErrorOut, "description": "El caso no existe"},
        409: {"model": ErrorOut, "description": "El cobro ya se recuperó"},
    },
)
async def reintentar(
    caso_id: str,
    response: Response,
    gestion: Gestion,
    gestor: GestorActual,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> ReintentoOut:
    salida = _reintento(await gestion.reintentar(caso_id, gestor, idempotency_key))
    if salida.repeticion:
        response.headers["Idempotent-Replay"] = "true"
    if salida.resultado == "EN_PROCESO":
        response.status_code = 202
    return salida

