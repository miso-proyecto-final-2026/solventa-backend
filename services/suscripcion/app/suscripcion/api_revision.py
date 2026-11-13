"""Revisión asistida (HU16): bandeja, detalle y decisión del asesor sobre los casos que la decisión automática derivó.

Todas las rutas exigen el JWT del asesor (ADR-0008, ADR-0020). La bandeja es compartida por todos los asesores.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.suscripcion.acceso import AsesorActual
from app.suscripcion.api_asistida import ClienteResumenOut
from app.suscripcion.revision import CategoriaMotivo, Prioridad, categorizar
from app.suscripcion.revision_asistida import (
    DetalleRevision,
    FilaRevision,
    ServicioRevisionAsistida,
    motivos_de,
)

router = APIRouter(prefix="/v1/suscripciones/revisiones", tags=["revision-asistida"])


def obtener_servicio_revision(request: Request) -> ServicioRevisionAsistida:
    return request.app.state.servicio_revision


Revisiones = Annotated[ServicioRevisionAsistida, Depends(obtener_servicio_revision)]


class DecisionRevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APROBADA", "RECHAZADA", "AJUSTADA"]
    justificacion: str = Field(min_length=20, max_length=2000, description="Interna: el cliente no la ve")
    monto_asegurado: int | None = Field(default=None, gt=0, description="El nuevo monto asegurado; solo al ajustar")

    @field_validator("justificacion", mode="before")
    @classmethod
    def _recortar(cls, valor: object) -> object:
        return valor.strip() if isinstance(valor, str) else valor

    @model_validator(mode="after")
    def _monto_solo_al_ajustar(self) -> Self:
        if self.decision == "AJUSTADA" and self.monto_asegurado is None:
            raise ValueError("monto_asegurado es obligatorio al ajustar")
        if self.decision != "AJUSTADA" and self.monto_asegurado is not None:
            raise ValueError("monto_asegurado solo se admite al ajustar")
        return self


class RevisionResumenOut(BaseModel):
    id: str = Field(description="El de la suscripción (saga)")
    cliente: ClienteResumenOut | None = Field(description="Nulo si MS Identidad no responde")
    producto: str
    moneda: str
    monto_asegurado: int
    categorias: list[CategoriaMotivo]
    motivo: str
    creada_en: datetime
    prioridad: Prioridad
    estado: Literal["PENDIENTE", "DECIDIDA"]
    decision: str | None
    cotizacion_vencida: bool
    vigente_hasta: datetime | None


class PaginaRevisionesOut(BaseModel):
    items: list[RevisionResumenOut]
    total: int
    total_vencidas: int = Field(description="Pendientes con la cotización vencida, para el aviso de la bandeja")
    pagina: int
    tamano: int


class MotivoOut(BaseModel):
    categoria: CategoriaMotivo
    texto: str


class CoberturaOut(BaseModel):
    tipo: str
    monto: int
    deducible: int


class CotizacionResumenOut(BaseModel):
    cotizacion_id: str
    producto: str
    moneda: str
    monto_asegurado: int
    prima_mensual: int
    prima_anual: int
    periodicidad_pago: str
    coberturas: list[CoberturaOut]
    vigente_hasta: datetime | None


class DecisionAutomaticaOut(BaseModel):
    motivos: list[MotivoOut]
    reglas_version: str | None
    decidido_en: datetime | None


class DecisionHumanaOut(BaseModel):
    decision: str
    justificacion: str | None
    asesor_id: str | None
    asesor_nombre: str | None
    decidido_en: datetime | None
    ajuste: dict | None


class RevisionDetalleOut(BaseModel):
    id: str
    cliente_id: str = Field(description="Para que la consola pida los niveles del perfil a Cotización")
    cliente: ClienteResumenOut | None
    cotizacion: CotizacionResumenOut
    decision_automatica: DecisionAutomaticaOut
    decision_humana: DecisionHumanaOut | None
    prioridad: Prioridad
    estado: Literal["PENDIENTE", "DECIDIDA"]
    creada_en: datetime
    cotizacion_vencida: bool
    puede_decidir: bool
    decisiones_permitidas: list[str]


def _cliente(f: FilaRevision) -> ClienteResumenOut | None:
    c = f.cliente
    if c is None:
        return None
    return ClienteResumenOut(
        nombre=c.nombre,
        inicial_apellido=c.apellido[:1],
        tipo_documento=c.tipo_documento,
        documento_enmascarado=c.documento_enmascarado,
    )


def _resumen(f: FilaRevision) -> RevisionResumenOut:
    s = f.suscripcion
    return RevisionResumenOut(
        id=s.id,
        cliente=_cliente(f),
        producto=s.producto,
        moneda=s.moneda,
        monto_asegurado=int(s.monto_asegurado),
        categorias=f.categorias,
        motivo=f.revision.motivo_derivacion,
        creada_en=f.revision.created_at,
        prioridad=f.prioridad,
        estado="PENDIENTE" if f.revision.decision is None else "DECIDIDA",
        decision=f.revision.decision,
        cotizacion_vencida=f.cotizacion_vencida,
        vigente_hasta=f.vigente_hasta,
    )


def _detalle(d: DetalleRevision) -> RevisionDetalleOut:
    s, r = d.suscripcion, d.revision
    return RevisionDetalleOut(
        id=s.id,
        cliente_id=s.cliente_id,
        cliente=_cliente(d),
        cotizacion=CotizacionResumenOut(
            cotizacion_id=s.cotizacion_id,
            producto=s.producto,
            moneda=s.moneda,
            monto_asegurado=int(s.monto_asegurado),
            prima_mensual=int(s.prima_mensual),
            prima_anual=int(s.prima_anual),
            periodicidad_pago=s.periodicidad_pago,
            coberturas=[
                CoberturaOut(tipo=c["tipo"], monto=int(c["monto"]), deducible=int(c.get("deducible", 0)))
                for c in (s.coberturas or [])
            ],
            vigente_hasta=d.vigente_hasta,
        ),
        decision_automatica=DecisionAutomaticaOut(
            motivos=[MotivoOut(categoria=categorizar(m), texto=m) for m in motivos_de(s)],
            reglas_version=s.reglas_version,
            decidido_en=s.decidido_en,
        ),
        decision_humana=None
        if r.decision is None
        else DecisionHumanaOut(
            decision=r.decision,
            justificacion=r.justificacion,
            asesor_id=r.asesor_id,
            asesor_nombre=r.asesor_nombre,
            decidido_en=r.decidido_en,
            ajuste=r.ajustes,
        ),
        prioridad=d.prioridad,
        estado="PENDIENTE" if r.decision is None else "DECIDIDA",
        creada_en=r.created_at,
        cotizacion_vencida=d.cotizacion_vencida,
        puede_decidir=d.puede_decidir,
        decisiones_permitidas=list(d.decisiones_permitidas),
    )


@router.get("", response_model=PaginaRevisionesOut, summary="Bandeja de revisión asistida")
async def bandeja(
    asesor: AsesorActual,
    servicio: Revisiones,
    estado: Annotated[Literal["PENDIENTE", "DECIDIDA"], Query()] = "PENDIENTE",
    categoria: Annotated[CategoriaMotivo | None, Query()] = None,
    mias: Annotated[bool, Query(description="Solo las cotizaciones de este asesor")] = False,
    vencidas: Annotated[bool, Query(description="Solo pendientes con la cotización vencida")] = False,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginaRevisionesOut:
    b = await servicio.listar(
        asesor, estado=estado, categoria=categoria, solo_mias=mias, vencidas=vencidas, pagina=pagina, tamano=tamano
    )
    return PaginaRevisionesOut(
        items=[_resumen(f) for f in b.filas], total=b.total, total_vencidas=b.total_vencidas, pagina=pagina, tamano=tamano
    )


@router.get("/{suscripcion_id}", response_model=RevisionDetalleOut, summary="Detalle de un caso derivado")
async def detalle(suscripcion_id: str, _asesor: AsesorActual, servicio: Revisiones) -> RevisionDetalleOut:
    return _detalle(await servicio.detalle(suscripcion_id))


@router.post("/{suscripcion_id}/decision", response_model=RevisionDetalleOut, summary="Aprobar, rechazar o ajustar un caso")
async def decidir(
    suscripcion_id: str, cuerpo: DecisionRevisionIn, asesor: AsesorActual, servicio: Revisiones
) -> RevisionDetalleOut:
    return _detalle(
        await servicio.decidir(asesor, suscripcion_id, cuerpo.decision, cuerpo.justificacion, cuerpo.monto_asegurado)
    )
