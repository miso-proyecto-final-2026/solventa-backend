"""Perfiles financieros demo por cliente para los simuladores (HU38).

Sin esto, los simuladores devuelven el mismo perfil neutro a todos y el precio no cambia con el
consentimiento. Con un archivo (`SIMULADOR_PERFILES_DEMO`) cada `cliente_id` recibe el perfil
que se le asignó; el que no aparece sigue recibiendo el neutro de siempre. Solo para
desarrollo y demostración: no hay datos reales.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

VARIABLE_ARCHIVO = "SIMULADOR_PERFILES_DEMO"


class CatalogoPerfilesDemo:
    def __init__(
        self,
        perfiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
        clientes: Mapping[str, str],
    ) -> None:
        desconocidos = {p for p in clientes.values() if p not in perfiles}
        if desconocidos:
            raise ValueError(f"perfiles demo sin definir: {sorted(desconocidos)}")
        self._perfiles = perfiles
        self._clientes = clientes

    @classmethod
    def desde_archivo(cls, ruta: Path) -> CatalogoPerfilesDemo:
        crudo = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
        return cls(crudo.get("perfiles", {}), crudo.get("clientes", {}))

    @classmethod
    def desde_entorno(
        cls, env: dict[str, str] | None = None
    ) -> CatalogoPerfilesDemo | None:
        """None si no hay archivo configurado: los simuladores quedan como siempre."""
        e = os.environ if env is None else env
        ruta = e.get(VARIABLE_ARCHIVO)
        return cls.desde_archivo(Path(ruta)) if ruta else None

    def para(self, cliente_id: str, fuente: str) -> dict[str, Any] | None:
        """Respuesta cruda del proveedor `fuente` para el cliente, o None si no está asignado."""
        nombre = self._clientes.get(cliente_id)
        if nombre is None:
            return None
        datos = self._perfiles[nombre].get(fuente)
        return None if datos is None else dict(datos)
