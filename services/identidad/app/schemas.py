from datetime import datetime

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
