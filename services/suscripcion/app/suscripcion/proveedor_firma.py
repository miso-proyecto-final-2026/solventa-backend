"""Puerto del proveedor de firma electrónica (ADR-0004) y su adaptador simulado (T4).

La autenticación reforzada del firmante es un código de un solo uso (OTP). El simulador acepta un código fijo configurable
(`FIRMA_SIM_OTP`) para poder probar en local y en staging; un proveedor real lo enviaría por SMS o correo. La biometría
queda fuera de esta entrega.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

NOMBRE_SIMULADO = "simulado"
METODO_OTP = "OTP"
OTP_POR_DEFECTO = "123456"


class ProveedorFirma(Protocol):
    nombre: str
    metodo_autenticacion: str

    async def solicitar_desafio(self, cliente_id: str) -> None:
        """Envía el código al firmante (el simulador no envía nada)."""
        ...

    async def verificar_desafio(self, cliente_id: str, codigo: str) -> bool: ...

    async def registrar_firma(self, cliente_id: str, documento_sha256: str) -> str:
        """Referencia de la firma en el proveedor."""
        ...


class ProveedorFirmaSimulado:
    nombre = NOMBRE_SIMULADO
    metodo_autenticacion = METODO_OTP

    def __init__(self, otp: str = OTP_POR_DEFECTO) -> None:
        self._otp = otp

    async def solicitar_desafio(self, cliente_id: str) -> None:
        return None

    async def verificar_desafio(self, cliente_id: str, codigo: str) -> bool:
        return hmac.compare_digest(codigo.encode(), self._otp.encode())

    async def registrar_firma(self, cliente_id: str, documento_sha256: str) -> str:
        return "simfirma_" + hashlib.sha256(f"{cliente_id}:{documento_sha256}".encode()).hexdigest()[:20]
