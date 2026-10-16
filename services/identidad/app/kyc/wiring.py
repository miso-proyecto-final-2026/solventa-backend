from __future__ import annotations

import os
from functools import lru_cache

import httpx

from app.kyc.almacen import construir_almacen
from app.kyc.config import ConfigKyc
from app.kyc.dominio import VerificadorIdentidad
from app.kyc.proveedor_b import ConfigProveedorB, VerificadorProveedorB
from app.kyc.servicio import ServicioVerificacion
from app.kyc.simulador import VerificadorSimulado


def construir_verificador(
    env: dict[str, str] | None = None, cliente_http: httpx.AsyncClient | None = None
) -> VerificadorIdentidad:
    """Elige el proveedor de KYC por configuración (`KYC_PROVEEDOR`), sin tocar el núcleo (HA06)."""
    e = os.environ if env is None else env
    proveedor = e.get("KYC_PROVEEDOR", "simulado").strip().lower()
    if proveedor == "simulado":
        return VerificadorSimulado()
    if proveedor == "proveedor_b":
        return VerificadorProveedorB(ConfigProveedorB.desde_entorno(e), cliente_http)
    raise ValueError(f"KYC_PROVEEDOR desconocido: {proveedor!r} (use 'simulado' o 'proveedor_b')")


@lru_cache(maxsize=1)
def obtener_servicio_kyc() -> ServicioVerificacion:
    """Dependencia de FastAPI. Las pruebas la sustituyen con `dependency_overrides`."""
    config = ConfigKyc.desde_entorno()
    return ServicioVerificacion(
        verificador=construir_verificador(),
        almacen=construir_almacen(config),
        config=config,
    )
