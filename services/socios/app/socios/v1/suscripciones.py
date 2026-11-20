"""Venta embebida, versión 1: suscripción, firma y consulta de la póliza (HU33).

El socio inicia la saga de HU15 a nombre de un cliente ya registrado, el cliente firma sin salir de su app y el socio consulta
hasta ver la póliza emitida. MS Socios no guarda estado de negocio: autentica, aplica idempotencia, identifica al cliente en
Identidad y traduce hacia Suscripción, que es dueña de la saga, la póliza y la firma (ADR-0026). Cada socio solo ve sus ventas:
la de otro responde 404, igual que una inexistente.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.socios.acceso import (
    ENDPOINT_POLIZAS_CONSULTAR,
    ENDPOINT_SUSCRIPCIONES_CREAR,
    ENDPOINT_SUSCRIPCIONES_FIRMAR,
    SocioAutorizado,
    exigir_endpoint,
)
from app.socios.almacen import ErrorAlmacen
from app.socios.idempotencia import huella
from app.socios.identificacion import ClienteIn, identificar_exigiendo
from app.socios.v1.api import ErrorOut, _clave_idempotencia

log = logging.getLogger(__name__)

VERSION = "v1"
router = APIRouter(prefix=f"/partners/{VERSION}", tags=["partners-v1"])

CrearSuscripcion = Annotated[SocioAutorizado, Depends(exigir_endpoint(ENDPOINT_SUSCRIPCIONES_CREAR, VERSION))]
FirmarSuscripcion = Annotated[SocioAutorizado, Depends(exigir_endpoint(ENDPOINT_SUSCRIPCIONES_FIRMAR, VERSION))]
ConsultarPoliza = Annotated[SocioAutorizado, Depends(exigir_endpoint(ENDPOINT_POLIZAS_CONSULTAR, VERSION))]
ClaveIdempotencia = Annotated[
    str | None,
    Header(alias="X-Idempotency-Key", description="Obligatoria. Repetirla devuelve la respuesta original."),
]


class SuscripcionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cotizacion_id: str = Field(min_length=1, max_length=36, description="Cotización vigente hecha con `cliente`.")
    cliente: ClienteIn
    metodo_pago: str = Field(
        min_length=1,
        max_length=200,
        description="Token de la pasarela que el socio obtuvo al tokenizar el medio de pago. Nunca el número de tarjeta.",
        examples=["tok_sim_0f8fad5b-d9cb-469f-a165-70867728950e"],
    )
    acepta_terminos: bool = Field(
        description="El socio declara que el cliente aceptó los términos. La prueba de aceptación es la firma con código (OTP)."
    )
    periodicidad_pago: Literal["ANUAL", "MENSUAL"] = "ANUAL"


class SuscripcionIniciadaOut(BaseModel):
    id: str = Field(description="Identificador de la venta. Se usa en la consulta, el documento y la firma.")
    cotizacion_id: str
    estado: str = Field(description="Estado de la saga: EN_PROGRESO, APROBADA, COBRO_REALIZADO, FIRMADA, EMITIDA...")
    paso_actual: str
    producto: str
    moneda: str
    monto_asegurado: int
    periodicidad_pago: str
    creada_en: datetime
    poliza_url: str = Field(description="Dónde consultar el resultado: `GET /partners/v1/polizas/{id}`.")


class DecisionOut(BaseModel):
    decision: str
    motivos: list[str]


class CobroOut(BaseModel):
    estado: str
    motivo: str | None


class FirmaEstadoOut(BaseModel):
    estado: str
    vence_en: datetime | None


class PolizaOut(BaseModel):
    numero_poliza: str
    estado: str
    vigencia_desde: datetime
    fecha_vencimiento: datetime
    fecha_emision: datetime | None
    documento_estado: str = Field(description="PENDIENTE mientras se genera el PDF; GENERADO cuando ya se puede firmar.")
    documento_sha256: str | None


class PolizaConsultaOut(BaseModel):
    id: str
    cotizacion_id: str
    estado: str = Field(description="Estado de la saga; `EMITIDA` cuando la póliza ya está vigente.")
    paso_actual: str
    pasos: list[str]
    producto: str
    moneda: str
    monto_asegurado: int
    periodicidad_pago: str
    decision: DecisionOut | None
    cobro: CobroOut | None
    firma: FirmaEstadoOut | None
    poliza: PolizaOut | None = Field(description="Nula hasta que el cobro se confirma y se genera la póliza.")
    creada_en: datetime


class DocumentoFirmaOut(BaseModel):
    """Lo que el cliente necesita para revisar y firmar. El PDF se descarga en `documento-firma/pdf`."""

    suscripcion_id: str
    numero_poliza: str
    documento_sha256: str = Field(description="Hash del PDF; se devuelve al firmar para probar qué firmó el cliente.")
    estado: Literal["PENDIENTE", "FIRMADA"]
    vence_en: datetime | None
    autenticacion: str = Field(description="Cómo se autentica el cliente al firmar (OTP).")


class FirmaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documento_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    documento_revisado: bool = Field(description="El cliente confirma que revisó el documento.")
    codigo: str = Field(min_length=1, max_length=12, description="Código de un solo uso (OTP) que recibió el cliente.")
    canal: Literal["MOVIL", "WEB"] = Field(description="Dónde firmó el cliente dentro del canal del socio.")
    dispositivo: str | None = Field(default=None, max_length=200, description="Dispositivo del cliente, para la evidencia.")
    ip_cliente: str | None = Field(default=None, max_length=45, description="IP del cliente, para la evidencia de no repudio.")


class FirmaOut(BaseModel):
    suscripcion_id: str
    estado: str = Field(description="Estado de la saga tras firmar; `EMITIDA` cuando la póliza quedó vigente.")
    documento_sha256: str
    firmada_en: datetime
    canal: str
    metodo_autenticacion: str


_ERRORES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut, "description": "Token ausente, inválido (INVALID_TOKEN) o vencido (TOKEN_EXPIRED)."},
    403: {"model": ErrorOut, "description": "El contrato no habilita el endpoint (ENDPOINT_NOT_AUTHORIZED)."},
    404: {"model": ErrorOut, "description": "La venta no existe o es de otro socio (RESOURCE_NOT_FOUND)."},
    409: {"model": ErrorOut, "description": "Conflicto con el estado de la venta o clave reutilizada (IDEMPOTENCY_CONFLICT)."},
    422: {"model": ErrorOut, "description": "Solicitud o cliente no válidos (INVALID_REQUEST); el motivo va en `details`."},
    429: {"model": ErrorOut, "description": "Cuota del contrato excedida (RATE_LIMIT_EXCEEDED)."},
    503: {"model": ErrorOut, "description": "Suscripción, Identidad u otra dependencia no responde (SERVICE_UNAVAILABLE)."},
}


def _poliza_url(suscripcion_id: str) -> str:
    return f"/partners/{VERSION}/polizas/{suscripcion_id}"


def a_salida_iniciada(s: dict[str, Any]) -> SuscripcionIniciadaOut:
    return SuscripcionIniciadaOut(
        id=s["id"],
        cotizacion_id=s["cotizacion_id"],
        estado=s["estado_saga"],
        paso_actual=s["paso_actual"],
        producto=s["producto"],
        moneda=s["moneda"],
        monto_asegurado=s["monto_asegurado"],
        periodicidad_pago=s["periodicidad_pago"],
        creada_en=s["creada_en"],
        poliza_url=_poliza_url(s["id"]),
    )


def a_salida_consulta(s: dict[str, Any]) -> PolizaConsultaOut:
    """Filtra los campos internos de Suscripción: ids de cobro y pasarela, ajustes, versión de reglas y claves."""
    decision, cobro, firma, poliza = s.get("decision"), s.get("cobro"), s.get("firma"), s.get("poliza")
    return PolizaConsultaOut(
        id=s["id"],
        cotizacion_id=s["cotizacion_id"],
        estado=s["estado_saga"],
        paso_actual=s["paso_actual"],
        pasos=s["pasos"],
        producto=s["producto"],
        moneda=s["moneda"],
        monto_asegurado=s["monto_asegurado"],
        periodicidad_pago=s["periodicidad_pago"],
        decision=None if decision is None else DecisionOut(decision=decision["decision"], motivos=decision["motivos"]),
        cobro=None if cobro is None else CobroOut(estado=cobro["estado"], motivo=cobro.get("motivo")),
        firma=None if firma is None else FirmaEstadoOut(estado=firma["estado"], vence_en=firma.get("vence_en")),
        poliza=None
        if poliza is None
        else PolizaOut(
            numero_poliza=poliza["numero_poliza"],
            estado=poliza["estado"],
            vigencia_desde=poliza["vigencia_desde"],
            fecha_vencimiento=poliza["fecha_vencimiento"],
            fecha_emision=poliza.get("fecha_emision"),
            documento_estado=poliza["documento_estado"],
            documento_sha256=poliza.get("documento_sha256"),
        ),
        creada_en=s["creada_en"],
    )


def a_salida_firma(f: dict[str, Any]) -> FirmaOut:
    return FirmaOut(
        suscripcion_id=f["suscripcion_id"],
        estado=f["estado_saga"],
        documento_sha256=f["documento_sha256"],
        firmada_en=f["firmada_en"],
        canal=f["canal"],
        metodo_autenticacion=f["metodo_autenticacion"],
    )


async def _idempotente(
    request: Request,
    socio: SocioAutorizado,
    clave: str | None,
    operacion: dict[str, Any],
    status: int,
    ejecutar: Callable[[], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    """Repetir la clave con el mismo cuerpo devuelve la respuesta original; con otro cuerpo es 409 (criterio 3).

    Si la operación falla se libera la reserva para que el socio pueda reintentar. Si Redis no deja guardar la respuesta, se
    entrega igual: Suscripción tiene su propia idempotencia, así que un reintento devuelve la misma saga o la misma firma.
    """
    clave = _clave_idempotencia(clave)
    estado = request.app.state
    huella_cuerpo = huella({"version": VERSION, **operacion})

    guardada = await estado.idempotencia.reservar(socio.client_id, clave, huella_cuerpo)
    if guardada is not None:
        log.info("idempotencia_repeticion", extra={"campos": {"evento": "idempotencia_repeticion", "client_id": socio.client_id}})
        return JSONResponse(guardada.cuerpo, status_code=guardada.status, headers={"X-Idempotent-Replayed": "true"})

    try:
        salida = await ejecutar()
    except BaseException:
        await estado.idempotencia.liberar(socio.client_id, clave)
        raise
    try:
        await estado.idempotencia.completar(socio.client_id, clave, huella_cuerpo, status, salida)
    except ErrorAlmacen:
        log.warning("idempotencia_sin_guardar", extra={"campos": {"evento": "idempotencia_sin_guardar", "client_id": socio.client_id}})
    return JSONResponse(salida, status_code=status)


@router.post(
    "/suscripciones",
    status_code=202,
    response_model=SuscripcionIniciadaOut,
    summary="Iniciar la venta de una cotización a un cliente registrado",
    responses=_ERRORES,
)
async def iniciar(
    solicitud: SuscripcionIn, request: Request, socio: CrearSuscripcion, x_idempotency_key: ClaveIdempotencia = None
) -> JSONResponse:
    clave = _clave_idempotencia(x_idempotency_key)
    estado = request.app.state

    async def ejecutar() -> dict[str, Any]:
        cliente_id = await identificar_exigiendo(estado.identidad, solicitud.cliente)
        creada = await estado.suscripciones.iniciar(
            socio.client_id,
            cliente_id,
            clave,
            cotizacion_id=solicitud.cotizacion_id,
            metodo_pago=solicitud.metodo_pago,
            acepta_terminos=solicitud.acepta_terminos,
            periodicidad_pago=solicitud.periodicidad_pago,
        )
        salida = a_salida_iniciada(creada).model_dump(mode="json")
        log.info(
            "suscripcion_socio",
            extra={"campos": {"evento": "suscripcion_socio", "client_id": socio.client_id, "suscripcion_id": salida["id"]}},
        )
        return salida

    return await _idempotente(
        request, socio, x_idempotency_key, {"operacion": ENDPOINT_SUSCRIPCIONES_CREAR, **solicitud.model_dump(mode="json")}, 202, ejecutar
    )


@router.get(
    "/polizas/{suscripcion_id}",
    response_model=PolizaConsultaOut,
    summary="Consultar el estado de la venta y de su póliza",
    responses=_ERRORES,
)
async def consultar(suscripcion_id: str, request: Request, socio: ConsultarPoliza) -> PolizaConsultaOut:
    """`suscripcion_id` es el `id` que devolvió `POST /suscripciones`. Cuando `estado` es `EMITIDA` trae la póliza vigente."""
    return a_salida_consulta(await request.app.state.suscripciones.detalle(suscripcion_id, socio.client_id))


@router.get(
    "/suscripciones/{suscripcion_id}/documento-firma",
    response_model=DocumentoFirmaOut,
    summary="Documento por firmar: su hash y vencimiento",
    responses=_ERRORES,
)
async def documento_firma(suscripcion_id: str, request: Request, socio: FirmarSuscripcion) -> DocumentoFirmaOut:
    d = await request.app.state.suscripciones.documento_firma(suscripcion_id, socio.client_id)
    return DocumentoFirmaOut(**{k: d[k] for k in DocumentoFirmaOut.model_fields})


@router.get(
    "/suscripciones/{suscripcion_id}/documento-firma/pdf",
    summary="PDF de la póliza por firmar, para que el cliente lo lea",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}, **_ERRORES},
)
async def documento_firma_pdf(suscripcion_id: str, request: Request, socio: FirmarSuscripcion) -> Response:
    contenido = await request.app.state.suscripciones.documento_pdf(suscripcion_id, socio.client_id)
    return Response(content=contenido, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post(
    "/suscripciones/{suscripcion_id}/firma/codigo",
    status_code=202,
    summary="Enviar al cliente el código de un solo uso para firmar",
    responses=_ERRORES,
)
async def solicitar_codigo(suscripcion_id: str, request: Request, socio: FirmarSuscripcion) -> Response:
    await request.app.state.suscripciones.solicitar_codigo(suscripcion_id, socio.client_id)
    return Response(status_code=202)


@router.post(
    "/suscripciones/{suscripcion_id}/firma",
    response_model=FirmaOut,
    summary="Firmar la póliza en nombre del cliente, con su código; repetirla devuelve la misma firma",
    responses=_ERRORES,
)
async def firmar(
    suscripcion_id: str,
    solicitud: FirmaIn,
    request: Request,
    socio: FirmarSuscripcion,
    x_idempotency_key: ClaveIdempotencia = None,
) -> JSONResponse:
    async def ejecutar() -> dict[str, Any]:
        f = await request.app.state.suscripciones.firmar(
            suscripcion_id,
            socio.client_id,
            {
                "documento_sha256": solicitud.documento_sha256,
                "documento_revisado": solicitud.documento_revisado,
                "codigo": solicitud.codigo,
                "canal": solicitud.canal,
                "dispositivo": solicitud.dispositivo,
                "ip": solicitud.ip_cliente,
            },
        )
        return a_salida_firma(f).model_dump(mode="json")

    # El código del cliente (OTP) solo entra en la huella (sha256): no se guarda ni se escribe en ningún log.
    return await _idempotente(
        request,
        socio,
        x_idempotency_key,
        {"operacion": ENDPOINT_SUSCRIPCIONES_FIRMAR, "suscripcion_id": suscripcion_id, **solicitud.model_dump(mode="json")},
        200,
        ejecutar,
    )
