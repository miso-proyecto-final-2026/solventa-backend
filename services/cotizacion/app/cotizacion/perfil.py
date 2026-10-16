"""Puerto del perfil y adaptador sobre el servicio de proveedores de HU09 (HU12).

El motor depende de `PerfilUnificado`. La normalización al esquema canónico vive
en `app.cotizacion.normalizacion` (HU10); este módulo solo la proyecta al contrato
del motor.

Los nombres crudos salen de los simuladores de HU09 y no coinciden con los de la
documentación (`score_crediticio`, `ingresos_mensuales`); al integrar un proveedor
real solo cambia su entrada en `NORMALIZADORES`.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.cotizacion.dominio import PerfilUnificado
from app.cotizacion.normalizacion import normalizar
from app.proveedores.dominio import PerfilCrudo
from app.proveedores.servicio import ServicioPerfil

log = logging.getLogger(__name__)


class PerfilPort(Protocol):
    async def obtener(self, cliente_id: str) -> PerfilUnificado: ...


def unificar(crudo: PerfilCrudo) -> PerfilUnificado:
    """Normaliza el perfil crudo (HU10) y lo proyecta al contrato del motor."""
    canonico = normalizar(crudo)
    # Un perfil degradado no aporta señales: se cotiza con la regla conservadora.
    senales = (
        {} if canonico.degradado else {n: c.valor for n, c in canonico.campos.items()}
    )
    return PerfilUnificado(
        cliente_id=canonico.cliente_id,
        senales=senales,
        degradado=canonico.degradado,
        con_consentimiento=canonico.con_consentimiento,
        hit_cache=canonico.hit_cache,
        fuentes=canonico.fuentes,
        completitud=canonico.completitud,
    )


class AdaptadorPerfilProveedores:
    def __init__(self, servicio: ServicioPerfil) -> None:
        self._servicio = servicio

    async def obtener(self, cliente_id: str) -> PerfilUnificado:
        try:
            return unificar(await self._servicio.obtener_perfil(cliente_id))
        except Exception:
            # Una falla inesperada del perfil no debe tumbar la cotización:
            # se cotiza con la regla conservadora (0 % de cotizaciones fallidas).
            log.exception("fallo al obtener el perfil; se cotiza degradado")
            return PerfilUnificado(cliente_id=cliente_id, degradado=True)
