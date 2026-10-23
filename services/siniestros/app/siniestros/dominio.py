"""Evento paramétrico de entrada y su validación (HU27, criterios 2 y 3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Tipos aceptados. El enunciado habla de vuelos y clima y la documentación técnica ejemplifica
# sismo, inundación y sequía: se aceptan ambos grupos hasta confirmar la lista final con producto.
TIPOS_EVENTO = frozenset(
    {
        "vuelo_retrasado",
        "vuelo_cancelado",
        "sismo",
        "inundacion",
        "sequia",
        "tormenta",
    }
)

_IDENTIFICADOR = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_BYTES_DATOS = 8192
# Un reloj del proveedor ligeramente adelantado no debe rechazar eventos reales.
TOLERANCIA_FUTURO = timedelta(minutes=5)


@dataclass(frozen=True)
class EventoEntrada:
    evento_id: str
    tipo: str
    fuente: str
    ocurrido_en: datetime
    datos: dict[str, Any]
    poliza_id: str | None


@dataclass(frozen=True)
class Defecto:
    campo: str
    motivo: str

    def como_dict(self) -> dict[str, str]:
        return {"campo": self.campo, "motivo": self.motivo}


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else None


def validar(
    cuerpo: Any, fuente_autenticada: str, ahora: datetime
) -> tuple[EventoEntrada | None, list[Defecto], str | None]:
    """Devuelve (evento, defectos, evento_id aprovechable). Con defectos, el evento es None."""
    if not isinstance(cuerpo, dict):
        return None, [Defecto("cuerpo", "debe ser un objeto JSON")], None

    defectos: list[Defecto] = []
    evento_id = cuerpo.get("evento_id")
    if not isinstance(evento_id, str) or not _IDENTIFICADOR.match(evento_id):
        defectos.append(
            Defecto("evento_id", "obligatorio: 1 a 64 letras, dígitos o . _ : -")
        )
        evento_id = None

    tipo = cuerpo.get("tipo")
    if tipo not in TIPOS_EVENTO:
        defectos.append(Defecto("tipo", f"debe ser uno de: {', '.join(sorted(TIPOS_EVENTO))}"))

    fuente = cuerpo.get("fuente")
    if not isinstance(fuente, str) or not fuente:
        defectos.append(Defecto("fuente", "obligatoria"))
    elif fuente != fuente_autenticada:
        # Una llave solo habla por su propia fuente: de lo contrario un proveedor podría
        # hacerse pasar por otro.
        defectos.append(Defecto("fuente", "fuente desconocida para esta llave"))

    ocurrido_en = _fecha(cuerpo.get("ocurrido_en"))
    if ocurrido_en is None:
        defectos.append(
            Defecto("ocurrido_en", "obligatoria: fecha ISO 8601 con zona horaria")
        )
    elif ocurrido_en > ahora + TOLERANCIA_FUTURO:
        defectos.append(Defecto("ocurrido_en", "no puede estar en el futuro"))

    datos = cuerpo.get("datos")
    if not isinstance(datos, dict) or not datos:
        defectos.append(Defecto("datos", "obligatorio: objeto con al menos un campo"))
    elif len(json.dumps(datos, ensure_ascii=False).encode()) > MAX_BYTES_DATOS:
        defectos.append(Defecto("datos", f"no puede superar {MAX_BYTES_DATOS} bytes"))

    poliza_id = cuerpo.get("poliza_id")
    if poliza_id is not None and (
        not isinstance(poliza_id, str) or not _IDENTIFICADOR.match(poliza_id)
    ):
        defectos.append(Defecto("poliza_id", "1 a 64 letras, dígitos o . _ : -"))

    if defectos:
        return None, defectos, evento_id
    assert ocurrido_en is not None and isinstance(datos, dict)
    return (
        EventoEntrada(
            evento_id=evento_id,  # type: ignore[arg-type]
            tipo=tipo,
            fuente=fuente,
            ocurrido_en=ocurrido_en.astimezone(UTC),
            datos=datos,
            poliza_id=poliza_id,
        ),
        [],
        evento_id,
    )
