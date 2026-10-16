"""Circuit breaker con sondeo half-open fuera del camino del usuario (HU09, criterio 5).

Transiciones: CERRADO → ABIERTO (N fallos consecutivos) → SEMIABIERTO (lo inicia
el sondeador en segundo plano) → CERRADO (sondeo exitoso) o ABIERTO (sondeo fallido).
Las solicitudes de usuario solo pasan con el circuito CERRADO, de modo que el
sondeo nunca consume una de ellas.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.proveedores.dominio import EstadoCircuito


class CircuitBreaker:
    def __init__(
        self,
        umbral_fallos: int = 5,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self._umbral = umbral_fallos
        self._reloj = reloj
        self._fallos = 0
        self._estado = EstadoCircuito.CERRADO
        self._abierto_desde: float | None = None

    @property
    def estado(self) -> EstadoCircuito:
        return self._estado

    @property
    def abierto_desde(self) -> float | None:
        return self._abierto_desde

    def permite_solicitud(self) -> bool:
        return self._estado is EstadoCircuito.CERRADO

    def registrar_exito(self) -> None:
        self._fallos = 0
        if self._estado is not EstadoCircuito.ABIERTO:
            self._estado = EstadoCircuito.CERRADO
            self._abierto_desde = None

    def registrar_fallo(self) -> None:
        self._fallos += 1
        if self._estado is EstadoCircuito.SEMIABIERTO or self._fallos >= self._umbral:
            self._abrir()

    def iniciar_sondeo(self) -> bool:
        """Pasa a SEMIABIERTO. Solo lo invoca el sondeador en segundo plano."""
        if self._estado is not EstadoCircuito.ABIERTO:
            return False
        self._estado = EstadoCircuito.SEMIABIERTO
        return True

    def resultado_sondeo(self, exito: bool) -> None:
        if self._estado is not EstadoCircuito.SEMIABIERTO:
            return
        if exito:
            self._fallos = 0
            self._estado = EstadoCircuito.CERRADO
            self._abierto_desde = None
        else:
            self._abrir()

    def _abrir(self) -> None:
        self._estado = EstadoCircuito.ABIERTO
        self._abierto_desde = self._reloj()
