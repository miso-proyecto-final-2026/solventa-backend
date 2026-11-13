"""API de conciliación de desembolsos de indemnizaciones para la consola de operaciones (HU31).

Solo el rol OPERADOR (JWT de Identidad, ADR-0008). Nunca devuelve la cuenta de destino y el cliente va enmascarado. No lleva
prefijo público todavía: el borde se abre en `solventa-infra` cuando la consola lo consuma.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.pagos.acceso import OperadorActual, exigir_operador
from app.pagos.api_gestion import ErrorOut
from app.pagos.conciliacion import (
    MAX_NOTA,
    Desembolso,
    Filtros,
    Resumen,
    ServicioConciliacion,
    a_csv,
    a_pdf,
)

TAMANO_MAXIMO = 100
_ERRORES = {
    401: {"model": ErrorOut, "description": "Falta el token, no es válido o venció (sesión vencida)."},
    403: {"model": ErrorOut, "description": "El token no es de un operador."},
}

router = APIRouter(
    prefix="/v1/pagos/conciliacion/desembolsos",
    tags=["conciliacion"],
    dependencies=[Depends(exigir_operador)],
    responses=_ERRORES,
)

EstadoConciliacionTexto = Literal["PENDIENTE", "CONCILIADO", "CON_DIFERENCIA"]
EstadoPagoTexto = Literal["PENDIENTE", "PAGADA", "FALLIDA"]


def obtener_conciliacion(request: Request) -> ServicioConciliacion:
    return request.app.state.servicio_conciliacion


Conciliacion = Annotated[ServicioConciliacion, Depends(obtener_conciliacion)]


class DesembolsoOut(BaseModel):
    id: str
    idempotency_key: str
    evento_id: str
    tipo_evento: str | None
    magnitud: dict[str, Any] | None
    ubicacion: str | None
    evento_ocurrido_en: datetime | None
    poliza_id: str
    producto: str | None
    cliente: str | None = Field(description="Enmascarado")
    numero_poliza: str | None
    monto_asegurado: Any | None
    validacion: dict[str, Any] | None
    estado: EstadoPagoTexto
    monto: Decimal
    moneda: str
    referencia_pasarela: str | None
    fecha_pago: datetime | None
    intentos: int
    reentregas: int = Field(description="Veces que llegó el mismo comando después del primero: ninguna volvió a pagar")
    estado_conciliacion: EstadoConciliacionTexto
    conciliado_por: str | None
    conciliado_en: datetime | None
    diferencia: dict[str, Any] | None
    escalada_en: datetime | None
    escalada_por: str | None
    nota_escalamiento: str | None
    codigo_fallo: str | None
    motivo_fallo: str | None


class PaginaOut(BaseModel):
    items: list[DesembolsoOut]
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


class DetalleOut(DesembolsoOut):
    intentos_de_pago: list[IntentoOut]


class ResumenOut(BaseModel):
    desembolsos: int
    claves_distintas: int
    claves_con_mas_de_un_pago: int = Field(description="Debe ser 0: la clave de idempotencia es única")
    pagados: int
    monto_pagado: Decimal
    por_estado_conciliacion: dict[str, int]


class EscalarIn(BaseModel):
    nota: str = Field(min_length=1, max_length=MAX_NOTA)


def _filtros(
    desde: Annotated[datetime | None, Query(description="Fecha del pago (o del registro si aún no se paga) desde")] = None,
    hasta: Annotated[datetime | None, Query()] = None,
    tipo_evento: Annotated[str | None, Query(max_length=40)] = None,
    estado_conciliacion: Annotated[EstadoConciliacionTexto | None, Query()] = None,
    estado: Annotated[EstadoPagoTexto | None, Query(description="Estado del pago")] = None,
) -> Filtros:
    return Filtros(desde, hasta, tipo_evento, estado_conciliacion, estado)


FiltrosActuales = Annotated[Filtros, Depends(_filtros)]


def _salida(d: Desembolso) -> dict[str, Any]:
    return dict(vars(d))


@router.get("", response_model=PaginaOut, summary="Desembolsos de indemnizaciones, con filtros")
async def listar(
    filtros: FiltrosActuales,
    servicio: Conciliacion,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=TAMANO_MAXIMO)] = 25,
) -> PaginaOut:
    items, total = servicio.listar(filtros, pagina=pagina, tamano=tamano)
    return PaginaOut(items=[DesembolsoOut(**_salida(d)) for d in items], total=total, pagina=pagina, tamano=tamano)


@router.get("/resumen", response_model=ResumenOut, summary="Totales y comprobación de que no hubo pagos duplicados")
async def resumen(filtros: FiltrosActuales, servicio: Conciliacion) -> Resumen:
    return servicio.resumen(filtros)


@router.get(
    "/exportar",
    summary="Exporta el reporte en CSV o PDF",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/pdf": {}}}},
)
async def exportar(
    filtros: FiltrosActuales,
    servicio: Conciliacion,
    formato: Annotated[Literal["csv", "pdf"], Query()] = "csv",
) -> Response:
    filas = servicio.filas_para_exportar(filtros)
    if formato == "csv":
        return Response(
            a_csv(filas),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="conciliacion-desembolsos.csv"'},
        )
    pdf = a_pdf(filas, servicio.resumen(filtros), servicio.ahora())
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="conciliacion-desembolsos.pdf"'},
    )


@router.get("/{desembolso_id}", response_model=DetalleOut, summary="Un desembolso con los intentos de pago")
async def detalle(desembolso_id: str, servicio: Conciliacion) -> DetalleOut:
    vista, historia = servicio.obtener(desembolso_id)
    return DetalleOut(**_salida(vista), intentos_de_pago=[IntentoOut(**h) for h in historia])


@router.post(
    "/{desembolso_id}/conciliar",
    response_model=DesembolsoOut,
    summary="Concilia el desembolso contra el extracto de la pasarela",
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut, "description": "No está pagado o la pasarela no tiene el pago."},
    },
)
async def conciliar(desembolso_id: str, operador: OperadorActual, servicio: Conciliacion) -> DesembolsoOut:
    return DesembolsoOut(**_salida(await servicio.conciliar(desembolso_id, operador)))


@router.post(
    "/{desembolso_id}/escalar",
    response_model=DesembolsoOut,
    summary="Escala al equipo de pagos un desembolso con diferencia",
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut, "description": "El desembolso no tiene diferencia."}},
)
async def escalar(desembolso_id: str, cuerpo: EscalarIn, operador: OperadorActual, servicio: Conciliacion) -> DesembolsoOut:
    return DesembolsoOut(**_salida(servicio.escalar(desembolso_id, operador, cuerpo.nota)))
