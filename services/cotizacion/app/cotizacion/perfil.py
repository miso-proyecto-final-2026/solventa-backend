"""Puerto del perfil y adaptador sobre el servicio de proveedores de HU09 (HU12).

El motor depende de `PerfilUnificado`. Mientras HU10 no entregue el perfil
canónico, este adaptador lo arma desde el perfil crudo de HU09; cuando HU10
llegue, solo se reemplaza este módulo.

PROVISIONAL: los nombres crudos salen de los simuladores de HU09 y no coinciden
con los de la documentación (`score_crediticio`, `ingresos_mensuales`). El
mapeo definitivo se acuerda al hacer HU10.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.cotizacion.dominio import PerfilUnificado
from app.proveedores.dominio import Fuente, PerfilCrudo
from app.proveedores.servicio import ServicioPerfil

log = logging.getLogger(__name__)

# campo crudo del proveedor -> nombre canónico de la señal
_MAPEO: dict[Fuente, dict[str, str]] = {
    Fuente.OPEN_FINANCE: {
        "monthlyIncomeCop": "ingresos_mensuales_cop",
        "debtRatio": "ratio_endeudamiento",
        "accountsOpenYears": "antiguedad_cuentas_anios",
    },
    Fuente.OPEN_DATA: {
        "estrato": "estrato",
        "reportes_negativos": "reportes_negativos",
    },
}


class PerfilPort(Protocol):
    async def obtener(self, cliente_id: str) -> PerfilUnificado: ...


def unificar(crudo: PerfilCrudo) -> PerfilUnificado:
    """Convierte el perfil crudo en el contrato del motor."""
    if crudo.por_defecto and not crudo.fuentes:
        # ServicioPerfil responde así solo cuando no hay consentimiento.
        return PerfilUnificado(cliente_id=crudo.cliente_id, con_consentimiento=False)
    senales: dict[str, int | float] = {}
    if not crudo.degradado:
        for fuente in crudo.fuentes:
            if fuente.por_defecto:
                continue
            for campo, canonico in _MAPEO.get(fuente.fuente, {}).items():
                valor = fuente.payload.get(campo)
                if isinstance(valor, int | float) and not isinstance(valor, bool):
                    senales[canonico] = valor
    return PerfilUnificado(
        cliente_id=crudo.cliente_id,
        senales=senales,
        degradado=crudo.degradado,
        hit_cache=bool(crudo.fuentes) and all(f.hit_cache for f in crudo.fuentes),
        fuentes=tuple(f.fuente.value for f in crudo.fuentes),
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
