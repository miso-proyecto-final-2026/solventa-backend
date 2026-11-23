"""API de socios, versión 1: `/partners/v1` (HU32, criterios 3, 6, 7 y 8).

Cada versión vive en su propio paquete y router: una `v2` se monta al lado sin tocar este archivo, y sus modelos de
entrada y salida son independientes (criterio 8). La salida expone solo lo que el socio necesita para mostrar el
precio; el desglose, la metadata interna y el origen se quedan en Cotización.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.socios.acceso import ENDPOINT_COTIZACIONES, SocioAutorizado, exigir_endpoint
from app.socios.almacen import ErrorAlmacen
from app.socios.errores import CATALOGO, MOTIVOS, FaltaClaveIdempotencia
from app.socios.idempotencia import DESCRIPCION_CLAVE_IDEMPOTENCIA, huella
from app.socios.identificacion import ClienteIn, identificar_exigiendo

log = logging.getLogger(__name__)

VERSION = "v1"
VERSIONES_SOPORTADAS = ("v1",)
# Espacio de nombres de los clientes finales de los socios: el `cliente_id` que recibe Cotización se deriva del socio
# y de su referencia, así no choca con los clientes propios de Solventa ni entre socios, y es estable.
ESPACIO_CLIENTES_SOCIOS = uuid.UUID("6f1c2a7e-3b0d-5c1e-9a4f-50c105000032")
_CLAVE_VALIDA = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TIMEOUT_ESTADO_S = 1.0

router = APIRouter(prefix=f"/partners/{VERSION}", tags=["partners-v1"])


class SolicitudCotizacionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    producto: str = Field(
        min_length=1,
        max_length=50,
        description="Producto a cotizar: `microseguro_vida` o `proteccion_dispositivos`. Los campos de cada uno van en `datos`.",
        examples=["microseguro_vida"],
    )
    referencia_cliente: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Identificador del cliente en el sistema del socio. No envíe documentos ni datos personales.",
        examples=["cli-000123"],
    )
    datos: dict[str, Any] = Field(
        description=(
            "Campos del producto, todos obligatorios. `microseguro_vida`: `edad` (entero, 18 a 65) y `suma_asegurada` "
            "(entero en COP, 5.000.000 a 100.000.000). `proteccion_dispositivos`: `tipo_dispositivo` (`celular`, `portatil` "
            "o `tableta`), `valor_dispositivo` (entero en COP, 300.000 a 15.000.000) y `antiguedad_meses` (entero, 0 a 36). "
            "Un campo que falta o fuera de rango responde 400 `INVALID_REQUEST` con `details`."
        ),
        examples=[{"edad": 35, "suma_asegurada": 50000000}],
    )
    cliente: ClienteIn | None = Field(
        default=None,
        description=(
            "Cliente registrado en Solventa (KYC verificado y consentimiento vigente). Con él la cotización se puede "
            "suscribir; sin él es informativa y no se puede suscribir. Con `cliente`, las condiciones se validan aquí mismo "
            "y otra vez al vender, en este orden: registro (`CLIENTE_NO_REGISTRADO`), KYC (`KYC_NO_VERIFICADO`) y "
            "consentimiento (`CONSENTIMIENTO_NO_VIGENTE`); la primera que falla responde 400 con ese `reason`."
        ),
    )


class CoberturaOut(BaseModel):
    tipo: str
    monto: int
    deducible: int


class CotizacionOut(BaseModel):
    id: str
    referencia_cliente: str
    producto: str
    moneda: str
    prima_anual: int
    prima_mensual: int
    coberturas: list[CoberturaOut]
    vigente_hasta: datetime
    degradado: bool = Field(description="True si se calculó con un perfil por defecto porque un proveedor no respondió.")
    aproximada: bool = Field(
        description=(
            "True si el precio no usó el perfil financiero del cliente (cotización sin `cliente`): puede cambiar al comprar."
        )
    )
    aviso: str | None = Field(default=None, description="Texto para mostrar al cliente cuando `aproximada` es true.")


class DependenciasOut(BaseModel):
    cotizacion: Literal["ok", "no_disponible"]
    base_datos: Literal["ok", "no_disponible"]
    almacen: Literal["ok", "no_disponible"]


class EstadoOut(BaseModel):
    estado: Literal["operativo", "degradado", "no_disponible"]
    version: str
    versiones_soportadas: list[str]
    dependencias: DependenciasOut
    timestamp: datetime


class DetalleErrorOut(BaseModel):
    campo: str
    motivo: str


class ErrorCuerpoOut(BaseModel):
    code: Literal[*CATALOGO] = Field(description="Código estable del catálogo de errores; programe contra él, no contra `message`.")  # type: ignore[valid-type]
    message: str = Field(description="Mensaje en español para una persona. Puede cambiar; no lo interprete.")
    trace_id: str = Field(description="Identificador de la solicitud. Repórtelo a soporte junto con `X-Correlation-Id`.")
    reason: Literal[*MOTIVOS] | None = Field(  # type: ignore[valid-type]
        default=None, description="Motivo de negocio, de una lista cerrada, cuando una regla no se cumple."
    )
    details: list[DetalleErrorOut] = Field(description="Campos inválidos, si aplica.")


class ErrorOut(BaseModel):
    error: ErrorCuerpoOut


_RETRY_AFTER = {"Retry-After": {"schema": {"type": "integer"}, "description": "Segundos para reintentar."}}


def respuestas_error(*, no_encontrado: bool = False, conflicto: bool = False) -> dict[int | str, dict[str, Any]]:
    """Las respuestas de error documentadas de una ruta de `/partners/v1`. Todas usan `ErrorOut` (catálogo de HU34)."""
    r: dict[int | str, dict[str, Any]] = {
        400: {
            "model": ErrorOut,
            "description": "INVALID_REQUEST: cuerpo, cabecera o parámetro inválido (`details`), o una regla de negocio no se cumple (`reason`).",
        },
        401: {"model": ErrorOut, "description": "INVALID_TOKEN (token ausente o inválido) o TOKEN_EXPIRED (vencido)."},
        403: {"model": ErrorOut, "description": "ENDPOINT_NOT_AUTHORIZED: el contrato no habilita el endpoint."},
        429: {
            "model": ErrorOut,
            "description": "RATE_LIMIT_EXCEEDED: cuota del contrato excedida. Reintente tras `Retry-After`.",
            "headers": _RETRY_AFTER,
        },
        500: {"model": ErrorOut, "description": "INTERNAL_ERROR: fallo no previsto. Reintente con retroceso exponencial."},
        503: {
            "model": ErrorOut,
            "description": (
                "SERVICE_UNAVAILABLE (una dependencia no responde; reintente con retroceso) o SERVICE_DEGRADED "
                "(circuito abierto; reintente tras `Retry-After`)."
            ),
            "headers": _RETRY_AFTER,
        },
    }
    if no_encontrado:
        r[404] = {"model": ErrorOut, "description": "RESOURCE_NOT_FOUND: el recurso no existe o es de otro socio."}
    if conflicto:
        r[409] = {
            "model": ErrorOut,
            "description": (
                "IDEMPOTENCY_CONFLICT: la clave se usó con otro cuerpo (no reintente con ella) o, con `reason` "
                "SOLICITUD_EN_PROCESO, la original sigue en curso (reintente con la misma clave tras `Retry-After`)."
            ),
            "headers": _RETRY_AFTER,
        }
    return r


_ERRORES_COTIZAR = respuestas_error(no_encontrado=True, conflicto=True)


def cliente_id_para(client_id: str, referencia_cliente: str) -> str:
    return str(uuid.uuid5(ESPACIO_CLIENTES_SOCIOS, f"{client_id}:{referencia_cliente}"))


def a_salida(cotizacion: dict[str, Any], referencia_cliente: str) -> CotizacionOut:
    return CotizacionOut(
        id=cotizacion["id"],
        referencia_cliente=referencia_cliente,
        producto=cotizacion["producto"],
        moneda=cotizacion["moneda"],
        prima_anual=cotizacion["prima_anual"],
        prima_mensual=cotizacion["prima_mensual"],
        coberturas=[CoberturaOut(**c) for c in cotizacion.get("coberturas", [])],
        vigente_hasta=cotizacion["vigente_hasta"],
        degradado=cotizacion.get("degradado", False),
        aproximada=cotizacion.get("aproximada", False),
        aviso=cotizacion.get("aviso"),
    )


def _clave_idempotencia(valor: str | None) -> str:
    if valor is None:
        raise FaltaClaveIdempotencia(detalles=[{"campo": "X-Idempotency-Key", "motivo": "requerida"}])
    if not _CLAVE_VALIDA.match(valor):
        raise FaltaClaveIdempotencia(
            "La cabecera X-Idempotency-Key no es válida.",
            detalles=[{"campo": "X-Idempotency-Key", "motivo": "1 a 128 caracteres: letras, dígitos, . _ : -"}],
        )
    return valor


@router.post(
    "/cotizaciones",
    status_code=201,
    response_model=CotizacionOut,
    summary="Cotizar un seguro para un cliente del socio",
    responses=_ERRORES_COTIZAR,
)
async def cotizar(
    solicitud: SolicitudCotizacionIn,
    request: Request,
    socio: Annotated[SocioAutorizado, Depends(exigir_endpoint(ENDPOINT_COTIZACIONES, VERSION))],
    x_idempotency_key: Annotated[
        str | None,
        Header(alias="X-Idempotency-Key", description=DESCRIPCION_CLAVE_IDEMPOTENCIA),
    ] = None,
) -> JSONResponse:
    clave = _clave_idempotencia(x_idempotency_key)
    estado = request.app.state
    cuerpo = solicitud.model_dump(mode="json")
    if cuerpo["cliente"] is None:
        del cuerpo["cliente"]  # la huella de una cotización informativa no cambia respecto a HU32
    huella_cuerpo = huella({"operacion": ENDPOINT_COTIZACIONES, "version": VERSION, **cuerpo})

    guardada = await estado.idempotencia.reservar(socio.client_id, clave, huella_cuerpo)
    if guardada is not None:
        log.info("idempotencia_repeticion", extra={"campos": {"evento": "idempotencia_repeticion", "client_id": socio.client_id}})
        return JSONResponse(guardada.cuerpo, status_code=guardada.status, headers={"X-Idempotent-Replayed": "true"})

    try:
        if solicitud.cliente is not None:
            cliente_id = await identificar_exigiendo(estado.identidad, solicitud.cliente)
        else:
            cliente_id = cliente_id_para(socio.client_id, solicitud.referencia_cliente)
        crudo = await estado.cotizaciones.crear(cliente_id, solicitud.producto, solicitud.datos)
    except BaseException:
        await estado.idempotencia.liberar(socio.client_id, clave)
        raise

    salida = a_salida(crudo, solicitud.referencia_cliente).model_dump(mode="json")
    try:
        await estado.idempotencia.completar(socio.client_id, clave, huella_cuerpo, 201, salida)
    except ErrorAlmacen:
        # La cotización ya existe en Cotización: se entrega. Un reintento con esta clave podría recalcular.
        log.warning("idempotencia_sin_guardar", extra={"campos": {"evento": "idempotencia_sin_guardar", "client_id": socio.client_id}})
    log.info(
        "cotizacion_socio",
        extra={"campos": {
            "evento": "cotizacion_socio",
            "client_id": socio.client_id,
            "cotizacion_id": salida["id"],
            "producto": salida["producto"],
            "degradado": salida["degradado"],
        }},
    )
    return JSONResponse(salida, status_code=201)


async def _con_tiempo(verificacion: Any) -> bool:
    try:
        return bool(await asyncio.wait_for(verificacion, _TIMEOUT_ESTADO_S))
    except Exception:  # noqa: BLE001  (timeout o fallo: la dependencia no está disponible)
        return False


@router.get(
    "/status",
    response_model=EstadoOut,
    summary="Estado del API de socios",
    responses={503: {"model": EstadoOut, "description": "El API no puede cotizar en este momento."}},
)
async def estado_api(request: Request) -> JSONResponse:
    """Público y sin datos sensibles: estado general y de cada dependencia, sin URLs, versiones internas ni errores."""
    estado = request.app.state
    cotizacion, base_datos, almacen = await asyncio.gather(
        _con_tiempo(estado.cotizaciones.disponible()),
        _con_tiempo(estado.registro.bd_disponible()),
        _con_tiempo(estado.almacen.disponible()),
    )
    # Sin Cotización no hay precio y sin el almacén no hay idempotencia (la escritura responde 503). Sin la base, los
    # tokens nuevos fallan pero los socios con token y contrato en caché siguen cotizando: degradado.
    if not (cotizacion and almacen):
        general = "no_disponible"
    elif not base_datos:
        general = "degradado"
    else:
        general = "operativo"
    cuerpo = EstadoOut(
        estado=general,
        version=VERSION,
        versiones_soportadas=list(VERSIONES_SOPORTADAS),
        dependencias=DependenciasOut(
            cotizacion="ok" if cotizacion else "no_disponible",
            base_datos="ok" if base_datos else "no_disponible",
            almacen="ok" if almacen else "no_disponible",
        ),
        timestamp=datetime.now(UTC),
    )
    return JSONResponse(cuerpo.model_dump(mode="json"), status_code=503 if general == "no_disponible" else 200)
