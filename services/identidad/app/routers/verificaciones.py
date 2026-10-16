"""Verificación de identidad (HU02). Las imágenes viajan por HTTPS y nunca se registran en logs."""

from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session

from app.auth import Acceso, acceso_actual, exigir_cliente
from app.db import get_db
from app.kyc.dominio import (
    EnRevision,
    EstadoKyc,
    ImagenesKyc,
    IntentosAgotados,
    RetoInvalido,
    YaVerificado,
)
from app.kyc.servicio import ServicioVerificacion
from app.kyc.wiring import obtener_servicio_kyc
from app.models import Cliente
from app.schemas import EstadoKycOut, VerificacionOut

router = APIRouter(prefix="/v1/clientes/{cliente_id}", tags=["verificacion"])

_FIRMAS = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)}
_MENSAJE_IMAGEN = "La imagen no es válida"


async def _leer_imagen(archivo: UploadFile, maximo: int) -> tuple[bytes, str]:
    tipo = (archivo.content_type or "").lower()
    if tipo not in _FIRMAS:
        raise HTTPException(status_code=415, detail="Solo se aceptan imágenes JPEG o PNG")
    contenido = await archivo.read(maximo + 1)
    if len(contenido) > maximo:
        raise HTTPException(status_code=413, detail="La imagen supera el tamaño permitido")
    if not contenido.startswith(_FIRMAS[tipo]):
        raise HTTPException(status_code=422, detail=_MENSAJE_IMAGEN)
    return contenido, tipo


@router.post("/verificaciones", response_model=VerificacionOut, status_code=201)
async def enviar_verificacion(
    cliente_id: str,
    request: Request,
    response: Response,
    reto: str = Form(...),
    documento_anverso: UploadFile = File(...),
    documento_reverso: UploadFile = File(...),
    selfie: UploadFile = File(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=100),
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
    servicio: ServicioVerificacion = Depends(obtener_servicio_kyc),
) -> VerificacionOut:
    if servicio.config.exigir_https and request.headers.get("x-forwarded-proto", "https") != "https":
        raise HTTPException(status_code=400, detail="Se requiere HTTPS")
    exigir_cliente(cliente_id, acceso)
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    maximo = servicio.config.max_bytes_imagen
    anverso, t_anverso = await _leer_imagen(documento_anverso, maximo)
    reverso, t_reverso = await _leer_imagen(documento_reverso, maximo)
    foto, t_selfie = await _leer_imagen(selfie, maximo)

    try:
        verificacion, creada = await servicio.verificar(
            db,
            cliente,
            idempotency_key,
            reto,
            ImagenesKyc(anverso=anverso, reverso=reverso, selfie=foto),
            {"anverso": t_anverso, "reverso": t_reverso, "selfie": t_selfie},
        )
    except RetoInvalido as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except (YaVerificado, EnRevision) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntentosAgotados as exc:
        segundos = max(1, int((exc.hasta - datetime.now(UTC)).total_seconds()))
        raise HTTPException(
            status_code=429,
            detail="Alcanzaste el máximo de intentos. Intenta de nuevo más tarde.",
            headers={"Retry-After": str(segundos)},
        ) from None

    if not creada:
        response.status_code = 200
    return VerificacionOut(
        id=verificacion.id,
        estado=verificacion.estado,
        motivo=verificacion.motivo,
        intentos_restantes=servicio.intentos_restantes(db, cliente_id),
        creado_en=verificacion.creado_en,
    )


@router.get("/verificacion", response_model=EstadoKycOut)
def consultar_verificacion(
    cliente_id: str,
    db: Session = Depends(get_db),
    acceso: Acceso = Depends(acceso_actual),
    servicio: ServicioVerificacion = Depends(obtener_servicio_kyc),
) -> EstadoKycOut:
    if not acceso.es_servicio:
        exigir_cliente(cliente_id, acceso)
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    ultima = servicio.ultima(db, cliente_id)
    motivo = ultima.motivo if ultima is not None and cliente.estado_kyc != EstadoKyc.VERIFICADO.value else None
    return EstadoKycOut(
        cliente_id=cliente_id,
        estado_kyc=cliente.estado_kyc,
        motivo=motivo,
        intentos_restantes=servicio.intentos_restantes(db, cliente_id),
        bloqueado_hasta=servicio.bloqueado_hasta(db, cliente_id),
        ultima_verificacion_id=ultima.id if ultima is not None else None,
    )
