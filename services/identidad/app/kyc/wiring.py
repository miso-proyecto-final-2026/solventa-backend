from __future__ import annotations

from functools import lru_cache

from app.kyc.almacen import construir_almacen
from app.kyc.config import ConfigKyc
from app.kyc.servicio import ServicioVerificacion
from app.kyc.simulador import VerificadorSimulado


@lru_cache(maxsize=1)
def obtener_servicio_kyc() -> ServicioVerificacion:
    """Dependencia de FastAPI. Las pruebas la sustituyen con `dependency_overrides`."""
    config = ConfigKyc.desde_entorno()
    return ServicioVerificacion(
        verificador=VerificadorSimulado(),
        almacen=construir_almacen(config),
        config=config,
    )
