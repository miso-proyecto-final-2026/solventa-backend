import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, field_validator

from app.validators import (
    validar_celular,
    validar_documento,
    validar_email,
    validar_password,
    validar_tipo_documento,
)


class ClienteCreate(BaseModel):
    nombre: str
    apellido: str
    tipo_documento: str
    numero_documento: str
    email: str
    celular: str
    password: str
    acepto_terminos: bool

    @field_validator("nombre", "apellido")
    @classmethod
    def _no_vacio(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("Este campo no puede estar vacío")
        return valor.strip()

    @field_validator("tipo_documento")
    @classmethod
    def _tipo_documento(cls, valor: str) -> str:
        return validar_tipo_documento(valor)

    @field_validator("numero_documento")
    @classmethod
    def _numero_documento(cls, valor: str) -> str:
        return validar_documento(valor)

    @field_validator("email")
    @classmethod
    def _email(cls, valor: str) -> str:
        return validar_email(valor)

    @field_validator("celular")
    @classmethod
    def _celular(cls, valor: str) -> str:
        return validar_celular(valor)

    @field_validator("password")
    @classmethod
    def _password(cls, valor: str) -> str:
        return validar_password(valor)

    @field_validator("acepto_terminos")
    @classmethod
    def _acepto_terminos(cls, valor: bool) -> bool:
        if not valor:
            raise ValueError("Debes aceptar la política de tratamiento de datos para continuar")
        return valor


class ClienteOut(BaseModel):
    id: str
    nombre: str
    apellido: str
    email: str
    estado_kyc: str
    access_token: str
    creado_en: datetime


class ErrorResponse(BaseModel):
    detail: str


class TipoConsentimiento(str, Enum):
    OPEN_FINANCE_OPEN_DATA = "OPEN_FINANCE_OPEN_DATA"


class EstadoConsentimiento(str, Enum):
    OTORGADO = "OTORGADO"
    REVOCADO = "REVOCADO"
    NO_OTORGADO = "NO_OTORGADO"


class CanalConsentimiento(str, Enum):
    MOVIL = "MOVIL"
    WEB = "WEB"


class ConsentimientoCreate(BaseModel):
    tipo: TipoConsentimiento = TipoConsentimiento.OPEN_FINANCE_OPEN_DATA
    version_texto: str
    canal: CanalConsentimiento = CanalConsentimiento.MOVIL

    @field_validator("version_texto")
    @classmethod
    def _version(cls, valor: str) -> str:
        if not re.fullmatch(r"v\d{1,3}", valor):
            raise ValueError("La versión del texto debe tener el formato v1, v2, …")
        return valor


class ConsentimientoOut(BaseModel):
    # Referencia al consentimiento vigente; la usa Cotización para la trazabilidad (HU11).
    id: str | None = None
    cliente_id: str
    tipo: TipoConsentimiento
    estado: EstadoConsentimiento
    version_texto: str | None = None
    fecha_otorgamiento: datetime | None = None
    fecha_revocacion: datetime | None = None


class VerificacionOut(BaseModel):
    id: str
    estado: str  # VERIFICADO | RECHAZADO | EN_REVISION
    motivo: str | None = None
    intentos_restantes: int
    creado_en: datetime


class EstadoKycOut(BaseModel):
    """Resultado que consume HU03: estado vigente, motivo genérico e intentos restantes."""

    cliente_id: str
    estado_kyc: str
    motivo: str | None = None
    intentos_restantes: int
    bloqueado_hasta: datetime | None = None
    ultima_verificacion_id: str | None = None


class LoginInternoIn(BaseModel):
    email: str
    password: str


class UsuarioInternoOut(BaseModel):
    id: str
    nombre: str
    rol: str


class SesionInternaOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expira_en: int  # segundos
    usuario: UsuarioInternoOut


class ConsentimientoResumenOut(BaseModel):
    estado: EstadoConsentimiento
    version_texto: str | None = None
    fecha_otorgamiento: datetime | None = None


class ClienteBusquedaOut(BaseModel):
    """Lo mínimo que necesita un asesor para identificar al cliente (sin email ni celular)."""

    cliente_id: str
    nombre: str
    apellido_inicial: str
    tipo_documento: str
    numero_documento: str
    estado_kyc: str
    consentimiento: ConsentimientoResumenOut


class SesionActualOut(BaseModel):
    """Solo el dueño del token: sin nombre, correo ni documento."""

    cliente_id: str


class ResumenClienteOut(BaseModel):
    """Para el documento de la póliza (HU20): nombre completo y documento enmascarado, sin correo ni celular."""

    cliente_id: str
    nombre: str
    apellido: str
    tipo_documento: str
    documento_enmascarado: str
