"""Validación de la condición paramétrica (HU28): del evento recibido a "corresponde pago" o "no corresponde".

Dos manejadores del consumidor idempotente de la plantilla (cada uno en su grupo, `solventa-siniestros.*`):

- `Evaluador.procesar` (grupo `recepcion`): por cada evento recibido busca las pólizas afectadas por lotes, evalúa
  la regla de su producto, guarda el resultado y, si corresponde pago, publica `indemnizacion_solicitada`.
- `RegistroPolizas.procesar` (grupo `polizas`): mantiene la copia local de pólizas con `poliza_emitida` y
  `poliza_cancelada`. Siniestros no lee la base de Suscripción (ADR-0001).

El manejador corre dentro de la transacción de la reserva de idempotencia. Si algo falla (incluida la publicación),
todo se deshace y el consumidor reintenta. Un comando puede llegar a salir dos veces solo si cae el proceso entre
publicar y confirmar; por eso su `idempotency_key` se deriva del par evento–póliza y MS Pagos descarta el duplicado.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.eventos import Evento, PublicadorEventos
from app.siniestros.modelos_db import EvaluacionParametricaDB, PolizaParametricaDB
from app.siniestros.parametrico import (
    NO_CORRESPONDE,
    Decision,
    Reglas,
    evaluar,
)

log = logging.getLogger("siniestros.evaluacion")

TOPIC_COMANDOS = "solventa.siniestros.comandos"  # clave de partición: evento_id (contrato de infra)
TIPO_COMANDO = "indemnizacion_solicitada"
GRUPO_POLIZAS = "solventa-siniestros.polizas"
TOPIC_CICLO_VIDA = "solventa.polizas.ciclovida"
FUENTE = "ms-siniestros"
TAMANO_LOTE = 500
# Namespace fijo: la misma pareja evento–póliza produce siempre el mismo id de comando.
_NAMESPACE_COMANDO = uuid.UUID("6f1c2f4e-5a53-4a0e-9c7b-1d6a9b1c2d3e")


def _utc(valor: datetime | None) -> datetime | None:
    return valor if valor is None or valor.tzinfo else valor.replace(tzinfo=UTC)


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return _utc(fecha)


def clave_idempotencia(evento_uuid: str, poliza_id: str) -> str:
    """Derivada del par evento–póliza: reentregas y reintentos producen la misma clave."""
    return f"{evento_uuid}:{poliza_id}"


def _vigente_en(poliza: PolizaParametricaDB, momento: datetime) -> str | None:
    """None si la póliza cubre `momento`; si no, el motivo por el que no."""
    cancelada = _utc(poliza.cancelada_en)
    if cancelada is not None and cancelada <= momento:
        return "poliza_cancelada"
    if momento < _utc(poliza.vigencia_desde):
        return "poliza_aun_no_vigente"
    if momento > _utc(poliza.fecha_vencimiento):
        return "poliza_vencida"
    return None


class Evaluador:
    def __init__(
        self,
        reglas: Reglas,
        publicador: PublicadorEventos,
        reloj: Callable[[], datetime] | None = None,
        tamano_lote: int = TAMANO_LOTE,
    ) -> None:
        self._reglas = reglas
        self._publicador = publicador
        self._reloj = reloj or (lambda: datetime.now(UTC))
        self._lote = tamano_lote

    async def procesar(self, evento: Evento, sesion: Session) -> None:
        p = evento.payload
        tipo, evento_uuid = p.get("tipo"), p.get("evento_uuid") or evento.event_id
        ocurrido_en = _fecha(p.get("ocurrido_en"))
        datos = p.get("datos")
        if not isinstance(tipo, str) or ocurrido_en is None or not isinstance(datos, dict):
            raise ValueError(f"evento {evento.event_id} sin tipo, fecha de ocurrencia o datos")

        reglas = {r.producto: r for r in self._reglas.para_evento(tipo)}
        if not reglas:
            log.info("sin reglas para el tipo=%s evento=%s", tipo, evento_uuid)
            return

        zona_evento = datos.get("zona") if isinstance(datos.get("zona"), str) else None
        poliza_id = p.get("poliza_id")
        evaluadas = pagos = 0
        if isinstance(poliza_id, str):
            poliza = sesion.get(PolizaParametricaDB, poliza_id)
            if poliza is None or poliza.producto not in reglas:
                self._guardar(sesion, evento_uuid, poliza_id, Decision(NO_CORRESPONDE, "poliza_no_aplicable", {}), None)
                evaluadas += 1
            else:
                pagos += await self._evaluar_poliza(
                    sesion, evento, evento_uuid, ocurrido_en, datos, zona_evento, reglas[poliza.producto], poliza
                )
                evaluadas += 1
        else:
            for poliza in self._polizas_afectadas(sesion, list(reglas)):
                pagos += await self._evaluar_poliza(
                    sesion, evento, evento_uuid, ocurrido_en, datos, zona_evento, reglas[poliza.producto], poliza
                )
                evaluadas += 1
        log.info("evento=%s tipo=%s evaluadas=%d pagos=%d", evento_uuid, tipo, evaluadas, pagos)

    def _polizas_afectadas(self, sesion: Session, productos: list[str]):
        """Pólizas de los productos afectados, por lotes con paginación por clave (criterio 6).

        La vigencia y la cancelación se juzgan contra la fecha del evento en `_vigente_en`, no aquí.
        """
        ultimo = ""
        while True:
            lote = sesion.scalars(
                select(PolizaParametricaDB)
                .where(
                    PolizaParametricaDB.producto.in_(productos),
                    PolizaParametricaDB.poliza_id > ultimo,
                )
                .order_by(PolizaParametricaDB.poliza_id)
                .limit(self._lote)
            ).all()
            if not lote:
                return
            yield from lote
            ultimo = lote[-1].poliza_id

    async def _evaluar_poliza(
        self,
        sesion: Session,
        evento: Evento,
        evento_uuid: str,
        ocurrido_en: datetime,
        datos: dict[str, Any],
        zona_evento: str | None,
        regla,
        poliza: PolizaParametricaDB,
    ) -> int:
        motivo_inactiva = _vigente_en(poliza, ocurrido_en)
        if motivo_inactiva is not None:
            decision = Decision(NO_CORRESPONDE, motivo_inactiva, {"regla": regla.id})
        else:
            decision = evaluar(regla, datos, zona_evento, poliza.zona)

        comando_id = None
        if decision.corresponde_pago:
            comando_id = str(uuid.uuid5(_NAMESPACE_COMANDO, clave_idempotencia(evento_uuid, poliza.poliza_id)))
        if not self._guardar(sesion, evento_uuid, poliza.poliza_id, decision, comando_id):
            return 0  # ya evaluada: no se emite un segundo comando (criterio 5)
        if comando_id is None:
            return 0
        await self._publicador.publicar(
            TOPIC_COMANDOS,
            Evento(
                event_id=comando_id,
                event_type=TIPO_COMANDO,
                source=FUENTE,
                correlation_id=evento.correlation_id,
                trace_id=evento.trace_id,
                payload={
                    "idempotency_key": clave_idempotencia(evento_uuid, poliza.poliza_id),
                    "evento_id": evento_uuid,
                    "poliza_id": poliza.poliza_id,
                    "producto": poliza.producto,
                    "monto_indemnizacion": decision.monto_indemnizacion,
                    "moneda": "COP",
                    "regla": regla.id,
                    "version_reglas": self._reglas.version,
                },
            ),
            clave=evento_uuid,
        )
        return 1

    def _guardar(
        self,
        sesion: Session,
        evento_uuid: str,
        poliza_id: str,
        decision: Decision,
        comando_id: str | None,
    ) -> bool:
        """Inserta la evaluación. False si el par evento–póliza ya estaba evaluado."""
        try:
            with sesion.begin_nested():
                sesion.add(
                    EvaluacionParametricaDB(
                        id=str(uuid.uuid4()),
                        evento_uuid=evento_uuid,
                        poliza_id=poliza_id,
                        resultado=decision.resultado,
                        motivo=decision.motivo,
                        valores=decision.valores,
                        version_reglas=self._reglas.version,
                        comando_id=comando_id,
                        evaluado_en=self._reloj(),
                    )
                )
                sesion.flush()
        except IntegrityError:
            log.info("par evento–póliza ya evaluado evento=%s poliza=%s", evento_uuid, poliza_id)
            return False
        return True


class RegistroPolizas:
    """Mantiene la copia local de las pólizas paramétricas a partir del ciclo de vida de la póliza."""

    def __init__(self, reglas: Reglas, reloj: Callable[[], datetime] | None = None) -> None:
        self._productos = reglas.productos
        self._reloj = reloj or (lambda: datetime.now(UTC))

    async def procesar(self, evento: Evento, sesion: Session) -> None:
        p = evento.payload
        if evento.event_type == "poliza_emitida":
            self._emitida(sesion, p)
        elif evento.event_type == "poliza_cancelada":
            self._cancelada(sesion, p, evento)

    def _emitida(self, sesion: Session, p: dict[str, Any]) -> None:
        producto = p.get("producto")
        if producto not in self._productos:
            return  # no es paramétrica: Siniestros no la necesita
        poliza_id = p.get("poliza_id")
        desde, hasta = _fecha(p.get("vigencia_desde")), _fecha(p.get("fecha_vencimiento"))
        if not isinstance(poliza_id, str) or desde is None or hasta is None:
            raise ValueError("poliza_emitida sin poliza_id o vigencia")
        zona = p.get("zona") if isinstance(p.get("zona"), str) else None
        existente = sesion.get(PolizaParametricaDB, poliza_id)
        if existente is None:
            sesion.add(
                PolizaParametricaDB(
                    poliza_id=poliza_id,
                    producto=producto,
                    zona=zona,
                    vigencia_desde=desde,
                    fecha_vencimiento=hasta,
                    actualizada_en=self._reloj(),
                )
            )
        else:
            existente.producto, existente.zona = producto, zona
            existente.vigencia_desde, existente.fecha_vencimiento = desde, hasta
            existente.actualizada_en = self._reloj()

    def _cancelada(self, sesion: Session, p: dict[str, Any], evento: Evento) -> None:
        poliza = sesion.get(PolizaParametricaDB, p.get("poliza_id") or "")
        if poliza is None:
            return
        poliza.cancelada_en = _fecha(p.get("fecha_cancelacion")) or _utc(evento.timestamp)
        poliza.actualizada_en = self._reloj()
