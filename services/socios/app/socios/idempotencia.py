"""Idempotencia de las escrituras de los socios con `X-Idempotency-Key` (HU32, criterio 6).

La clave es por socio (dos socios pueden usar la misma sin cruzarse) y se guarda solo su sha256. Flujo:

1. `reservar`: `SET NX` de la clave en estado `en_proceso` con un TTL corto. Si ya existía:
   - con otro cuerpo → 409 `IDEMPOTENCY_CONFLICT` (no reintentar con esa clave);
   - todavía en proceso → 409 con `reason: SOLICITUD_EN_PROCESO` y `Retry-After` (sí reintentar, con la misma clave);
   - terminada con el mismo cuerpo → se devuelve la respuesta original sin recalcular.
2. `completar`: guarda el estado y el cuerpo de la respuesta exitosa por 24 horas.
3. `liberar`: si la solicitud falló (4xx/5xx), se borra la reserva para que el socio pueda reintentar.

Guardar la respuesta 24 horas en Redis es estado técnico con vencimiento, no un registro de negocio: la cotización
vive en Cotización (criterio 9, ADR-0025). Si Redis no responde, la idempotencia falla cerrada (503): sin ella no se
puede garantizar que un reintento no recalcule.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.socios.almacen import Almacen, ErrorAlmacen
from app.socios.errores import (
    ConflictoIdempotencia,
    ServicioNoDisponible,
    SolicitudEnProceso,
)

PREFIJO = "socios:idem"
EN_PROCESO = "en_proceso"
COMPLETADA = "completada"


@dataclass(frozen=True)
class RespuestaGuardada:
    status: int
    cuerpo: Any


def huella(cuerpo: Any) -> str:
    """sha256 del cuerpo en JSON canónico: el mismo contenido da la misma huella sin importar el orden de las claves."""
    return hashlib.sha256(json.dumps(cuerpo, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Idempotencia:
    def __init__(self, almacen: Almacen, ttl_s: int = 24 * 3600, ttl_en_proceso_s: int = 30) -> None:
        self._almacen = almacen
        self._ttl_s = ttl_s
        self._ttl_en_proceso_s = ttl_en_proceso_s

    @staticmethod
    def _clave(client_id: str, clave: str) -> str:
        return f"{PREFIJO}:{client_id}:{hashlib.sha256(clave.encode()).hexdigest()}"

    async def reservar(self, client_id: str, clave: str, huella_cuerpo: str) -> RespuestaGuardada | None:
        """None si la solicitud es nueva y queda reservada; la respuesta original si es una repetición."""
        llave = self._clave(client_id, clave)
        reserva = json.dumps({"estado": EN_PROCESO, "huella": huella_cuerpo})
        try:
            for _ in range(2):  # la segunda vuelta cubre que la entrada venza entre el SET NX y el GET
                if await self._almacen.crear_si_no_existe(llave, reserva, self._ttl_en_proceso_s):
                    return None
                guardado = await self._almacen.leer(llave)
                if guardado is not None:
                    break
            else:
                raise SolicitudEnProceso
        except ErrorAlmacen as error:
            raise ServicioNoDisponible("No se puede verificar la clave de idempotencia; reintente.") from error

        entrada = json.loads(guardado)
        if entrada.get("huella") != huella_cuerpo:
            raise ConflictoIdempotencia()
        if entrada.get("estado") != COMPLETADA:
            raise SolicitudEnProceso
        return RespuestaGuardada(status=int(entrada["status"]), cuerpo=entrada["cuerpo"])

    async def completar(self, client_id: str, clave: str, huella_cuerpo: str, status: int, cuerpo: Any) -> None:
        """Puede lanzar ErrorAlmacen: quien llama entrega la respuesta igual y lo registra (ver v1/api.py)."""
        valor = json.dumps({"estado": COMPLETADA, "huella": huella_cuerpo, "status": status, "cuerpo": cuerpo}, default=str)
        await self._almacen.escribir(self._clave(client_id, clave), valor, self._ttl_s)

    async def liberar(self, client_id: str, clave: str) -> None:
        try:
            await self._almacen.borrar(self._clave(client_id, clave))
        except ErrorAlmacen:
            pass  # la reserva vence sola en `ttl_en_proceso_s`
