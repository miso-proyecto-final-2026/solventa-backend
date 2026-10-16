"""Adaptador del proveedor de KYC «B» (HA06): otro contrato detrás del mismo puerto.

El contrato de B difiere del simulado original en todo lo que le importa a un adaptador:
- Autenticación: cabecera `X-Api-Key`.
- Ruta y formato: `POST /v2/identity-checks` con JSON (`reference`, `challenge`, `documents`, `selfie`
  en base64), en lugar de los campos `match`/`liveness` del simulador.
- Resultado: `outcome` = `PASS` | `FAIL` | `REVIEW` y `id` del caso.
- Errores: `{"error": {"code", "message"}}` con códigos HTTP 4xx/5xx.

Todo ese mapeo vive aquí: el núcleo (`ServicioVerificacion`) no lo conoce.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx

from app.kyc.dominio import (
    RETO_VIDA,
    DecisionProveedor,
    ProveedorKycError,
    ResultadoVerificacion,
    SolicitudVerificacion,
)

RUTA = "/v2/identity-checks"

# Vocabulario de retos de B; un reto que B no conoce se envía tal cual y B no lo superará.
_RETOS = {RETO_VIDA: "TURN_LEFT"}


@dataclass(frozen=True)
class ConfigProveedorB:
    base_url: str
    api_key: str
    timeout_s: float = 4.0  # por debajo del timeout duro del núcleo (KYC_TIMEOUT_S)

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigProveedorB:
        e = os.environ if env is None else env
        faltan = [v for v in ("KYC_PROVEEDOR_B_URL", "KYC_PROVEEDOR_B_API_KEY") if not e.get(v)]
        if faltan:
            raise ValueError(f"Falta configurar {', '.join(faltan)} para usar el proveedor B")
        return cls(
            base_url=e["KYC_PROVEEDOR_B_URL"],
            api_key=e["KYC_PROVEEDOR_B_API_KEY"],
            timeout_s=float(e.get("KYC_PROVEEDOR_B_TIMEOUT_S", cls.timeout_s)),
        )


class VerificadorProveedorB:
    """Adaptador de `VerificadorIdentidad` sobre la API HTTP del proveedor B."""

    def __init__(self, config: ConfigProveedorB, cliente: httpx.AsyncClient | None = None) -> None:
        self._clave = config.api_key
        self._http = cliente or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"), timeout=config.timeout_s
        )

    async def verificar(self, solicitud: SolicitudVerificacion) -> ResultadoVerificacion:
        try:
            respuesta = await self._http.post(
                RUTA, json=self._cuerpo(solicitud), headers={"X-Api-Key": self._clave}
            )
        except httpx.HTTPError as exc:
            raise ProveedorKycError(f"El proveedor B no respondió ({type(exc).__name__})") from exc
        return self._mapear(respuesta)

    @staticmethod
    def _cuerpo(solicitud: SolicitudVerificacion) -> dict:
        def b64(contenido: bytes) -> str:
            return base64.b64encode(contenido).decode("ascii")

        imagenes = solicitud.imagenes
        return {
            "reference": solicitud.clave_idempotencia,
            "challenge": _RETOS.get(solicitud.reto, solicitud.reto),
            "documents": {"front": b64(imagenes.anverso), "back": b64(imagenes.reverso)},
            "selfie": b64(imagenes.selfie),
        }

    @staticmethod
    def _mapear(respuesta: httpx.Response) -> ResultadoVerificacion:
        if respuesta.status_code >= 400:
            raise ProveedorKycError(
                f"El proveedor B respondió {respuesta.status_code} ({_codigo_de_error(respuesta)})"
            )
        try:
            cuerpo = respuesta.json()
            resultado = cuerpo["outcome"]
            referencia = str(cuerpo["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProveedorKycError("Respuesta inválida del proveedor B") from exc
        if resultado == "PASS":
            return ResultadoVerificacion(DecisionProveedor.APROBADA, referencia)
        if resultado == "FAIL":
            return ResultadoVerificacion(DecisionProveedor.RECHAZADA, referencia)
        # REVIEW (o un valor nuevo): B no concluyó. Se degrada como cualquier fallo del proveedor,
        # que deja el caso EN_REVISION; el dominio no necesita un estado más.
        raise ProveedorKycError(f"Resultado no concluyente del proveedor B ({resultado})")

    async def cerrar(self) -> None:
        await self._http.aclose()


def _codigo_de_error(respuesta: httpx.Response) -> str:
    try:
        return str(respuesta.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return "sin código"
