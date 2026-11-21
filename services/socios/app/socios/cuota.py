"""Cuota de solicitudes por segundo de cada socio (HU32, criterio 5).

El borde es API Gateway HTTP (v2), sin planes de uso por cliente: la cuota del contrato se aplica aquí (ADR-0025).
Ventana fija de un segundo en Redis: `INCR socios:cuota:<client_id>:<segundo>` con vencimiento. Es compartida entre
réplicas y cuesta un viaje a Redis por solicitud.

Si Redis no responde, la cuota falla abierta: se deja pasar y se registra. Cortar a todos los socios porque el
contador no está disponible sería peor que permitir un exceso momentáneo; el WAF sigue limitando por IP.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.socios.almacen import Almacen, ErrorAlmacen

log = logging.getLogger(__name__)

PREFIJO = "socios:cuota"
VENTANA_S = 1


class LimitadorCuota:
    def __init__(self, almacen: Almacen, reloj: Callable[[], float] = time.time) -> None:
        self._almacen = almacen
        self._reloj = reloj

    async def consumir(self, client_id: str, cuota_rps: int) -> int | None:
        """Cuenta la solicitud. Devuelve None si está dentro de la cuota o los segundos para reintentar si la excede."""
        ahora = self._reloj()
        segundo = int(ahora)
        try:
            usadas = await self._almacen.incrementar(f"{PREFIJO}:{client_id}:{segundo}", ttl_s=VENTANA_S + 1)
        except ErrorAlmacen as error:
            log.warning(
                "cuota_sin_almacen",
                extra={"campos": {"evento": "cuota_sin_almacen", "client_id": client_id, "error": str(error)}},
            )
            return None
        if usadas <= cuota_rps:
            return None
        log.info(
            "cuota_excedida",
            extra={"campos": {"evento": "cuota_excedida", "client_id": client_id, "cuota_rps": cuota_rps}},
        )
        # Retry-After es un entero de segundos (RFC 9110): la ventana se renueva en el próximo segundo.
        return VENTANA_S
