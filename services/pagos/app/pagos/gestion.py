"""Gestión de cobros fallidos desde la consola web (HU19): bandeja, detalle, notas, reintento y aviso al operador.

- Un **caso** se abre cuando un cobro de prima se rechaza (`servicio._actualizar_caso`, en la misma transacción) y guarda
  la fecha límite de la gracia. Se cierra como `RECUPERADO` cuando un reintento se aprueba.
- El **reintento** es un cobro nuevo de la misma suscripción con el token ya registrado del cliente y la clave
  `cobro_prima:<suscripcion_id>:<n>`. El caso se bloquea mientras se prepara: dos clics a la vez no crean dos cobros; si
  ya hay un reintento en curso, o la solicitud repite su `Idempotency-Key`, se devuelve el mismo.
- El **detector** marca `VENCIDO` cada caso pendiente cuya gracia terminó y encola el aviso al operador
  (`cobro_gracia_vencida` en `solventa.notificaciones`) en la misma transacción: se avisa una sola vez.

Nada de esto devuelve el token ni datos de contacto del cliente (ADR-0008); de la tarjeta, solo marca y últimos 4.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.eventos import Evento
from app.pagos import repositorio as repo
from app.pagos.acceso import Gestor
from app.pagos.config import TOPIC_NOTIFICACIONES
from app.pagos.dominio import (
    ESTADOS_CASO_ABIERTOS,
    ORIGEN_GESTION,
    EstadoCaso,
    EstadoTransaccion,
    parece_pan,
)
from app.pagos.errores import (
    CasoNoEncontrado,
    CasoYaRecuperado,
    DatoDeTarjetaNoPermitido,
    SolicitudInvalida,
)
from app.pagos.modelos_db import (
    CasoCobroFallidoDB,
    IntentoCobroDB,
    NotaCobroFallidoDB,
    TokenTarjetaDB,
    TransaccionDB,
)
from app.pagos.servicio import FUENTE, ComandoCobro, ServicioCobros

log = logging.getLogger("pagos.gestion")

GRACIA_VENCIDA = "cobro_gracia_vencida"
LARGO_NOTA = 1000


def _ahora() -> datetime:
    return datetime.now(UTC)


def clave_reintento(suscripcion_id: str, numero: int) -> str:
    return f"cobro_prima:{suscripcion_id}:{numero}"


@dataclass(frozen=True)
class CasoVista:
    caso: CasoCobroFallidoDB
    token: TokenTarjetaDB | None
    estado_gracia: str | None  # VIGENTE | POR_VENCER | VENCIDA; None si ya se recuperó


@dataclass(frozen=True)
class PaginaCasos:
    casos: list[CasoVista]
    total: int
    pagina: int
    tamano: int


@dataclass(frozen=True)
class DetalleCaso:
    vista: CasoVista
    transacciones: list[tuple[TransaccionDB, list[IntentoCobroDB]]]
    notas: list[NotaCobroFallidoDB]


@dataclass(frozen=True)
class ResultadoReintento:
    caso: CasoCobroFallidoDB
    transaccion: TransaccionDB
    numero: int
    creado: bool               # False: dos clics o un reintento ya en curso, no se cobró otra vez
    ultimo_intento: str | None  # resultado del último intento ante la pasarela (p. ej. NO_DISPONIBLE)


class ServicioGestion:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        cobros: ServicioCobros,
        horas_por_vencer: int,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._cobros = cobros
        self._por_vencer = timedelta(hours=horas_por_vencer)
        self._reloj = reloj

    # --- consultas ------------------------------------------------------------------------------------------------

    def estado_gracia(self, caso: CasoCobroFallidoDB, ahora: datetime) -> str | None:
        if caso.estado == EstadoCaso.RECUPERADO.value:
            return None
        limite = repo.utc(caso.gracia_hasta)
        if limite <= ahora:
            return "VENCIDA"
        return "POR_VENCER" if limite - ahora < self._por_vencer else "VIGENTE"

    def bandeja(self, *, estados: list[str] | None, solo_por_vencer: bool, pagina: int, tamano: int) -> PaginaCasos:
        """Casos sin recuperar por defecto, los de gracia más cercana primero (criterios 1 y 2)."""
        ahora = self._reloj()
        with self._fabrica() as s:
            casos, total = repo.buscar_casos(
                s,
                estados=estados or list(ESTADOS_CASO_ABIERTOS),
                gracia_antes_de=ahora + self._por_vencer if solo_por_vencer else None,
                desplazamiento=(pagina - 1) * tamano,
                limite=tamano,
            )
            tokens = repo.tokens_por_id(s, {c.token_id for c in casos})
            vistas = [CasoVista(c, tokens.get(c.token_id), self.estado_gracia(c, ahora)) for c in casos]
        return PaginaCasos(vistas, total, pagina, tamano)

    def detalle(self, caso_id: str) -> DetalleCaso:
        """Historial de cobros e intentos de la suscripción y las notas del caso (criterio 3)."""
        ahora = self._reloj()
        with self._fabrica() as s:
            caso = repo.caso(s, caso_id)
            if caso is None:
                raise CasoNoEncontrado
            token = s.get(TokenTarjetaDB, caso.token_id)
            transacciones = [(tx, repo.intentos_de(s, tx.id)) for tx in repo.transacciones_de(s, caso.suscripcion_id)]
            notas = repo.notas_de(s, caso_id)
        return DetalleCaso(CasoVista(caso, token, self.estado_gracia(caso, ahora)), transacciones, notas)

    # --- notas ----------------------------------------------------------------------------------------------------

    def agregar_nota(self, caso_id: str, texto: str, autor: Gestor) -> NotaCobroFallidoDB:
        texto = texto.strip()
        if not 1 <= len(texto) <= LARGO_NOTA:
            raise SolicitudInvalida(detalles=[{"campo": "texto", "motivo": f"debe tener de 1 a {LARGO_NOTA} caracteres"}])
        if parece_pan(texto):
            raise DatoDeTarjetaNoPermitido("La nota no puede llevar números de tarjeta.")
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            if repo.caso(s, caso_id) is None:
                raise CasoNoEncontrado
            nota = NotaCobroFallidoDB(
                caso_id=caso_id,
                autor_id=autor.id,
                autor_nombre=autor.nombre or autor.id,
                autor_rol=autor.rol,
                texto=texto,
                created_at=ahora,
            )
            s.add(nota)
            s.flush()
        # El texto no va al log: puede tener datos del cliente.
        log.info("nota agregada caso_id=%s autor_id=%s rol=%s", caso_id, autor.id, autor.rol)
        return nota

    # --- reintento ------------------------------------------------------------------------------------------------

    async def reintentar(self, caso_id: str, autor: Gestor, clave_solicitud: str | None = None) -> ResultadoReintento:
        """Cobra otra vez con el token registrado (criterio 4). Idempotente frente a dos clics."""
        if clave_solicitud is not None and not 1 <= len(clave_solicitud) <= 100:
            raise SolicitudInvalida(detalles=[{"campo": "Idempotency-Key", "motivo": "debe tener de 1 a 100 caracteres"}])
        transaccion_id, numero, creado = await asyncio.to_thread(self._preparar, caso_id, clave_solicitud)
        log.info(
            "reintento solicitado caso_id=%s transaccion_id=%s reintento=%d creado=%s autor_id=%s rol=%s",
            caso_id, transaccion_id, numero, creado, autor.id, autor.rol,
        )
        if creado:
            await self._cobros.procesar(transaccion_id)
        return await asyncio.to_thread(self._resultado, caso_id, transaccion_id, numero, creado)

    def _preparar(self, caso_id: str, clave_solicitud: str | None) -> tuple[str, int, bool]:
        with self._fabrica() as s, s.begin():
            caso = repo.caso(s, caso_id, bloquear=True)
            if caso is None:
                raise CasoNoEncontrado
            if caso.estado == EstadoCaso.RECUPERADO.value:
                raise CasoYaRecuperado
            vigente = repo.obtener(s, caso.transaccion_vigente_id) if caso.transaccion_vigente_id else None
            if vigente is not None and (
                vigente.estado == EstadoTransaccion.PENDIENTE.value
                or (clave_solicitud is not None and clave_solicitud == caso.clave_solicitud)
            ):
                return vigente.id, caso.reintentos, False
            original = repo.obtener(s, caso.id)
            numero = caso.reintentos + 1
            comando = ComandoCobro.validado(
                idempotency_key=clave_reintento(caso.suscripcion_id, numero),
                suscripcion_id=caso.suscripcion_id,
                cliente_id=caso.cliente_id,
                token=s.get(TokenTarjetaDB, caso.token_id).token,
                monto=caso.monto,
                moneda=caso.moneda,
                periodicidad_pago=original.periodicidad_pago if original is not None else None,
            )
            tx, creada = self._cobros.registrar(s, comando, origen=ORIGEN_GESTION)
            caso.reintentos = numero
            caso.transaccion_vigente_id = tx.id
            caso.clave_solicitud = clave_solicitud
            caso.updated_at = self._reloj()
            return tx.id, numero, creada

    def _resultado(self, caso_id: str, transaccion_id: str, numero: int, creado: bool) -> ResultadoReintento:
        with self._fabrica() as s:
            caso = repo.caso(s, caso_id)
            tx = repo.obtener(s, transaccion_id)
            intentos = repo.intentos_de(s, transaccion_id)
        return ResultadoReintento(caso, tx, numero, creado, intentos[-1].resultado if intentos else None)

    # --- detector de gracia vencida (criterio 6) ------------------------------------------------------------------

    def vencer_casos(self, limite: int = 50) -> int:
        """Marca VENCIDO los casos con la gracia terminada y encola el aviso al operador. Seguro entre réplicas."""
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            casos = repo.casos_con_gracia_vencida(s, ahora, limite)
            for caso in casos:
                caso.estado = EstadoCaso.VENCIDO.value
                caso.operador_notificado_en = ahora
                caso.updated_at = ahora
                self._encolar_aviso(s, caso, ahora)
        for caso in casos:
            log.info("gracia vencida caso_id=%s suscripcion_id=%s: aviso al operador encolado", caso.id, caso.suscripcion_id)
        return len(casos)

    def _encolar_aviso(self, s: Session, caso: CasoCobroFallidoDB, ahora: datetime) -> None:
        payload: dict[str, Any] = {
            "caso_id": caso.id,
            "suscripcion_id": caso.suscripcion_id,
            "cliente_id": caso.cliente_id,
            "monto": str(Decimal(caso.monto).quantize(Decimal("0.01"))),
            "moneda": caso.moneda,
            "gracia_hasta": repo.utc(caso.gracia_hasta).isoformat(),
            "reintentos": caso.reintentos,
            "destinatario_rol": "OPERADOR",
            "accion_sugerida": "EVALUAR_SUSPENSION_COBERTURA",
        }
        evento = Evento(
            event_type=GRACIA_VENCIDA,
            source=FUENTE,
            timestamp=ahora,
            correlation_id=caso.suscripcion_id,
            trace_id=str(uuid4()),
            payload=payload,
        )
        repo.encolar_evento(
            s, event_id=evento.event_id, topic=TOPIC_NOTIFICACIONES, clave=caso.cliente_id,
            payload=evento.model_dump(mode="json"), ahora=ahora,
        )

    async def ejecutar_detector(self, intervalo_s: float = 60.0) -> None:
        while True:
            try:
                await asyncio.to_thread(self.vencer_casos)
                await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el detector de gracia vencida")
                await asyncio.sleep(intervalo_s)
