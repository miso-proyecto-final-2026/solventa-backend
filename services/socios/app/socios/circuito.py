"""Circuit breaker de las dependencias de MS Socios (HU34, criterio 5).

Mismo patrón que `services/cotizacion/app/proveedores/circuit_breaker.py` (no hay librería compartida entre servicios):
CERRADO → ABIERTO tras N fallos seguidos → SEMIABIERTO al vencer el enfriamiento → CERRADO con un éxito, o ABIERTO otra vez.
Aquí el sondeo es la primera solicitud real tras el enfriamiento: solo una pasa; las demás reciben 503 SERVICE_DEGRADED.

Es uno por dependencia y vive en memoria de cada pod: no se comparte en Redis para no sumar latencia ni otro punto de falla.
Cuenta como fallo un timeout, un error de conexión o un 5xx; un 4xx no (el servicio respondió).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable
from enum import Enum

import httpx

from app.socios.errores import ServicioDegradado, ServicioNoDisponible
from app.socios.traduccion import NOMBRES, es_fallo_de_dependencia

log = logging.getLogger(__name__)

NOTA_PERFIL_POR_DEFECTO = " La cotización puede volver con un perfil por defecto (campo `degradado`)."


class EstadoCircuito(str, Enum):
    CERRADO = "cerrado"
    ABIERTO = "abierto"
    SEMIABIERTO = "semiabierto"


class Circuito:
    def __init__(
        self,
        servicio: str,
        umbral_fallos: int = 5,
        enfriamiento_s: float = 30.0,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self.servicio = servicio
        self._umbral = umbral_fallos
        self._enfriamiento_s = enfriamiento_s
        self._reloj = reloj
        self._fallos = 0
        self._estado = EstadoCircuito.CERRADO
        self._abierto_desde = 0.0

    @property
    def estado(self) -> EstadoCircuito:
        return self._estado

    def _espera_restante_s(self) -> int:
        return max(1, math.ceil(self._abierto_desde + self._enfriamiento_s - self._reloj()))

    def _degradado(self) -> ServicioDegradado:
        espera = self._espera_restante_s() if self._estado is EstadoCircuito.ABIERTO else 1
        mensaje = f"{NOMBRES[self.servicio]} está degradada; reintente en {espera} s."
        if self.servicio == "cotizacion":
            mensaje += NOTA_PERFIL_POR_DEFECTO
        return ServicioDegradado(mensaje, espera)

    def _admitir(self) -> None:
        """Deja pasar la solicitud o lanza 503 SERVICE_DEGRADED sin llamar a la dependencia."""
        if self._estado is EstadoCircuito.CERRADO:
            return
        if self._estado is EstadoCircuito.ABIERTO and self._reloj() >= self._abierto_desde + self._enfriamiento_s:
            self._estado = EstadoCircuito.SEMIABIERTO  # esta solicitud es el sondeo
            return
        raise self._degradado()

    def _exito(self) -> None:
        if self._estado is not EstadoCircuito.CERRADO:
            log.warning(
                "circuito_cerrado", extra={"campos": {"evento": "circuito_cerrado", "dependencia": self.servicio}}
            )
        self._fallos = 0
        self._estado = EstadoCircuito.CERRADO

    def _fallo(self) -> None:
        self._fallos += 1
        if self._estado is EstadoCircuito.SEMIABIERTO or self._fallos >= self._umbral:
            self._estado = EstadoCircuito.ABIERTO
            self._abierto_desde = self._reloj()
            log.warning(
                "circuito_abierto", extra={"campos": {"evento": "circuito_abierto", "dependencia": self.servicio}}
            )

    async def ejecutar(self, llamada: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """La respuesta de la dependencia (de cualquier estado HTTP), 503 SERVICE_DEGRADED con el circuito abierto, o
        503 SERVICE_UNAVAILABLE si la dependencia no responde."""
        self._admitir()
        try:
            r = await llamada()
        except httpx.HTTPError as error:
            self._fallo()
            raise ServicioNoDisponible(f"{NOMBRES[self.servicio]} no responde; reintente más tarde.") from error
        except BaseException:
            # Una cancelación en el sondeo no puede dejar el circuito semiabierto para siempre.
            if self._estado is EstadoCircuito.SEMIABIERTO:
                self._estado = EstadoCircuito.ABIERTO
            raise
        if es_fallo_de_dependencia(r):
            self._fallo()
        else:
            self._exito()
        return r
