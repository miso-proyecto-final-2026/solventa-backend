"""Dobles para probar el cobro sin red: un reloj controlable y una pasarela en memoria que implementa el puerto.

Las pruebas del caso de uso usan `PasarelaFalsa`, no el simulador: así no cambian si cambia el adaptador (EC-M2.2).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.eventos import Evento
from app.pagos.dominio import (
    DatosToken,
    PasarelaNoDisponible,
    PasarelaSinRespuesta,
    ResultadoCobro,
    SolicitudCobro,
    TokenDesconocido,
)

AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "tok_sim_prueba01"


class Reloj:
    def __init__(self, inicio: datetime = AHORA) -> None:
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


class PasarelaFalsa:
    """Pasarela en memoria, idempotente por clave. `guion` fija lo que pasa en cada orden de cobro, en orden:
    "aprobar", "rechazar", "perder" (cobra y no responde), "colgar" (cobra y tarda), "caida" (no recibe la orden).
    Agotado el guion, aprueba."""

    def __init__(self, *guion: str) -> None:
        self.guion = list(guion)
        self.libro: dict[str, ResultadoCobro] = {}
        self.ordenes: list[SolicitudCobro] = []
        self.consultas: list[str] = []
        self.reembolsos: dict[str, ResultadoCobro] = {}
        self.ordenes_reembolso: list[SolicitudCobro] = []
        self.reembolso_caido = False

    @property
    def cargos(self) -> int:
        return sum(1 for r in self.libro.values() if r.aprobado)

    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        self.ordenes.append(solicitud)
        if solicitud.idempotency_key in self.libro:
            return self.libro[solicitud.idempotency_key]
        paso = self.guion.pop(0) if self.guion else "aprobar"
        if paso == "caida":
            raise PasarelaNoDisponible("conexión rechazada")
        if paso == "rechazar":
            resultado = ResultadoCobro(aprobado=False, referencia_pasarela="ref-r", codigo="FONDOS_INSUFICIENTES", motivo="Sin fondos")
        else:
            resultado = ResultadoCobro(aprobado=True, referencia_pasarela=f"ref-{len(self.libro) + 1}")
        self.libro[solicitud.idempotency_key] = resultado
        if paso == "perder":
            raise PasarelaSinRespuesta("respuesta perdida")
        if paso == "colgar":
            await asyncio.sleep(60)
        return resultado

    async def reembolsar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        self.ordenes_reembolso.append(solicitud)
        if self.reembolso_caido:
            raise PasarelaNoDisponible("conexión rechazada")
        if solicitud.idempotency_key not in self.reembolsos:
            self.reembolsos[solicitud.idempotency_key] = ResultadoCobro(
                aprobado=True, referencia_pasarela=f"rf-{len(self.reembolsos) + 1}"
            )
        return self.reembolsos[solicitud.idempotency_key]

    async def consultar(self, idempotency_key: str) -> ResultadoCobro | None:
        self.consultas.append(idempotency_key)
        return self.libro.get(idempotency_key)

    async def describir_token(self, token: str) -> DatosToken:
        if not token.startswith("tok_"):
            raise TokenDesconocido
        return DatosToken(ultimos4="4242", marca="VISA")


def comando(suscripcion_id: str = "susc-1", **cambios) -> dict:
    """Payload de `cobro_prima_solicitado` tal como lo publica MS Suscripción (HU18)."""
    payload = {
        "suscripcion_id": suscripcion_id,
        "cotizacion_id": "cot-1",
        "cliente_id": "cli-1",
        "decision": "APROBADA",
        "moneda": "COP",
        "prima_mensual": 14583,
        "prima_anual": 175000,
        "monto": "175000.00",
        "periodicidad_pago": "ANUAL",
        "idempotency_key": f"cobro_prima:{suscripcion_id}",
        "metodo_pago": TOKEN,
    }
    payload.update(cambios)
    return {k: v for k, v in payload.items() if v is not None}


def evento(payload: dict | None = None, event_type: str = "cobro_prima_solicitado") -> Evento:
    payload = payload or comando()
    return Evento(event_type=event_type, source="ms-suscripcion", correlation_id=payload.get("suscripcion_id", "x"), payload=payload)


MONTO = Decimal("175000.00")


def comando_reembolso(suscripcion_id: str = "susc-1", **cambios) -> dict:
    """Payload de `reembolso_cobro_solicitado` tal como lo publica MS Suscripción (HU21)."""
    payload = {
        "suscripcion_id": suscripcion_id,
        "cliente_id": "cli-1",
        "cobro_transaccion_id": "tx-1",
        "monto": "175000.00",
        "moneda": "COP",
        "motivo": "FIRMA_VENCIDA",
        "idempotency_key": f"reembolso_prima:{suscripcion_id}",
    }
    payload.update(cambios)
    return {k: v for k, v in payload.items() if v is not None}


def evento_reembolso(payload: dict | None = None) -> Evento:
    payload = payload or comando_reembolso()
    return evento(payload, event_type="reembolso_cobro_solicitado")
