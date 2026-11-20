"""Dobles de prueba de MS Socios: Cotización falsa y un almacén que siempre falla."""

from __future__ import annotations

from typing import Any

from app.socios.almacen import ErrorAlmacen
from app.socios.errores import ErrorSocios

# Solo para pruebas: nunca es un secreto real.
SECRETO_PRUEBA = "secreto-de-prueba-socio-0123456789"
SECRETO_JWT_PRUEBA = "jwt-de-prueba-socios-0123456789-abcdefghij"


def cotizacion_de(cliente_id: str, producto: str, numero: int = 1) -> dict[str, Any]:
    """Lo que responde `POST /v1/cotizaciones` de Cotización (CotizacionOut), con todos sus campos internos."""
    return {
        "id": f"00000000-0000-4000-8000-{numero:012d}",
        "cliente_id": cliente_id,
        "producto": producto,
        "estado": "EMITIDA",
        "moneda": "COP",
        "prima_anual": 120000,
        "prima_mensual": 10000,
        "coberturas": [{"tipo": "muerte", "monto": 50000000, "deducible": 0}],
        "vigente_hasta": "2026-12-17T00:00:00Z",
        "version_reglas": "2026.10",
        "degradado": False,
        "aproximada": False,
        "aviso": None,
        "componentes": [{"concepto": "prima_base", "monto": 120000}],
        "metadata": {"hit_cache": True, "latencia_interna_ms": 12.5},
        "asesor_id": None,
        "sin_cuenta": False,
    }


class CotizacionesFalsas:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, str, dict[str, Any]]] = []
        self.error: ErrorSocios | None = None
        self.arriba = True

    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]:
        self.llamadas.append((cliente_id, producto, datos))
        if self.error is not None:
            raise self.error
        return cotizacion_de(cliente_id, producto, len(self.llamadas))

    async def disponible(self) -> bool:
        return self.arriba


class AlmacenCaido:
    async def incrementar(self, clave: str, ttl_s: int) -> int:
        raise ErrorAlmacen("ConnectionError")

    async def crear_si_no_existe(self, clave: str, valor: str, ttl_s: int) -> bool:
        raise ErrorAlmacen("ConnectionError")

    async def leer(self, clave: str) -> str | None:
        raise ErrorAlmacen("ConnectionError")

    async def escribir(self, clave: str, valor: str, ttl_s: int) -> None:
        raise ErrorAlmacen("ConnectionError")

    async def borrar(self, clave: str) -> None:
        raise ErrorAlmacen("ConnectionError")

    async def disponible(self) -> bool:
        return False

    async def cerrar(self) -> None:
        return None
