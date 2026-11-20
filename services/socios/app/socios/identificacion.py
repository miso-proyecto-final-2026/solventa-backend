"""Identificación del cliente del socio en MS Identidad (HU33, criterio 5).

Hoy el socio solo puede vender a clientes que ya existen en Solventa: con KYC `VERIFICADO` y el consentimiento Open Finance
vigente. Dar de alta clientes nuevos desde el canal del socio queda como gap (ADR-0026). El documento viaja a Identidad en
el cuerpo de la llamada y nunca se escribe en un log ni se guarda aquí.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.socios.clientes import Identidad
from app.socios.errores import SolicitudInvalida

KYC_VERIFICADO = "VERIFICADO"

CLIENTE_NO_REGISTRADO = "CLIENTE_NO_REGISTRADO"
KYC_NO_VERIFICADO = "KYC_NO_VERIFICADO"
CONSENTIMIENTO_NO_VIGENTE = "CONSENTIMIENTO_NO_VIGENTE"


class ClienteIn(BaseModel):
    """El cliente del socio, por su documento. Debe estar registrado en Solventa."""

    model_config = ConfigDict(extra="forbid")

    tipo_documento: Literal["CC", "CE", "PASAPORTE"] = Field(examples=["CC"])
    numero_documento: str = Field(
        pattern=r"^[A-Za-z0-9]{5,15}$", description="5 a 15 caracteres alfanuméricos.", examples=["100200300"]
    )


def _no_apto(motivo: str, mensaje: str) -> SolicitudInvalida:
    return SolicitudInvalida(mensaje, detalles=[{"campo": "cliente", "motivo": motivo}])


async def identificar_exigiendo(identidad: Identidad, cliente: ClienteIn) -> str:
    """`cliente_id` real del cliente, o 422 con el motivo si no existe, no tiene KYC verificado o no dio su consentimiento."""
    encontrado = await identidad.identificar(cliente.tipo_documento, cliente.numero_documento)
    if encontrado is None:
        raise _no_apto(CLIENTE_NO_REGISTRADO, "El cliente no está registrado en Solventa.")
    if encontrado.get("estado_kyc") != KYC_VERIFICADO:
        raise _no_apto(KYC_NO_VERIFICADO, "El cliente no ha verificado su identidad (KYC) en Solventa.")
    if not encontrado.get("consentimiento_vigente"):
        raise _no_apto(CONSENTIMIENTO_NO_VIGENTE, "El cliente no tiene un consentimiento Open Finance vigente.")
    return str(encontrado["cliente_id"])
