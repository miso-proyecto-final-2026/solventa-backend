"""Casos de uso de Notificaciones (HU22): registrar los envíos de un evento y entregarlos con reintentos.

Dos momentos, cada uno en su transacción, como el cobro de Pagos (ADR-0014) y el documento de la póliza (ADR-0017):

1. **Registrar** (en la transacción del consumidor): una fila `envios` por canal, con la restricción única
   (`evento_origen_id`, `canal`). Reentregar `poliza_emitida` no crea nada (criterio 5). No llama a ningún proveedor:
   si la transacción se deshiciera, un correo ya enviado no se desharía.
2. **Entregar** (proceso de fondo): toma los pendientes con `SKIP LOCKED`, anota el intento y reserva la fila antes de
   llamar al canal. Un fallo se reintenta con espera creciente; agotados los intentos, el envío queda `FALLIDO` y
   visible al operador. **Nada de esto toca la póliza**, que ya está emitida (criterio 6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.contexto import correlacion_id
from app.eventos import Evento
from app.notificaciones import plantillas
from app.notificaciones import repositorio as repo
from app.notificaciones.clientes import Certificados, Identidad
from app.notificaciones.dominio import (
    Canal,
    CanalCorreo,
    CanalPush,
    ErrorEnvio,
    EstadoEnvio,
    TipoNotificacion,
)
from app.notificaciones.modelos_db import EnvioDB

log = logging.getLogger("notificaciones")

POLIZA_EMITIDA = "poliza_emitida"
CAMPOS_REQUERIDOS = ("poliza_id", "suscripcion_id", "cliente_id", "numero_poliza", "producto", "vigencia_desde", "fecha_vencimiento")


def _ahora() -> datetime:
    return datetime.now(UTC)


def _fecha(valor: str) -> datetime:
    fecha = datetime.fromisoformat(str(valor))
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Pendiente:
    """Lo que el envío necesita, leído antes de salir a la red (la sesión ya está cerrada)."""

    id: str
    canal: str
    cliente_id: str
    idioma: str
    datos: dict[str, Any]


class ServicioNotificaciones:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        identidad: Identidad,
        certificados: Certificados,
        correo: CanalCorreo,
        push: CanalPush,
        url_publica: str | None = None,
        max_intentos: int = 5,
        espera_base_s: float = 5.0,
        espera_maxima_s: float = 300.0,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._identidad = identidad
        self._certificados = certificados
        self._correo = correo
        self._push = push
        self._url_publica = url_publica
        self._max_intentos = max_intentos
        self._base_s = espera_base_s
        self._maxima_s = espera_maxima_s
        self._reloj = reloj

    # --- 1. registrar ---------------------------------------------------------------------------------------------

    async def manejar_evento(self, evento: Evento, sesion: Session) -> None:
        """Manejador del consumidor de `solventa.polizas.ciclovida` (corre en la transacción de la plantilla)."""
        if evento.event_type != POLIZA_EMITIDA:
            log.info("evento ignorado event_type=%s event_id=%s", evento.event_type, evento.event_id)
            return
        faltan = [c for c in CAMPOS_REQUERIDOS if not evento.payload.get(c)]
        if faltan:
            # Un evento mal formado no mejora al reintentarlo: el consumidor lo manda a la cola de fallidos.
            raise ValueError(f"poliza_emitida sin: {', '.join(faltan)}")
        datos = {c: str(evento.payload[c]) for c in CAMPOS_REQUERIDOS}
        cliente_id = datos["cliente_id"]
        idioma = repo.idioma_de(sesion, cliente_id)
        ahora = self._reloj()
        for canal in (Canal.PUSH, Canal.CORREO):
            creado = repo.crear_envio(
                sesion,
                EnvioDB(
                    id=str(uuid4()),
                    evento_origen_id=evento.event_id,
                    tipo=TipoNotificacion.POLIZA_EMITIDA.value,
                    canal=canal.value,
                    cliente_id=cliente_id,
                    idioma=idioma,
                    datos=datos,
                    estado=EstadoEnvio.PENDIENTE.value,
                    intentos=0,
                    proximo_intento_en=ahora,
                    correlacion_id=evento.correlation_id or correlacion_id.get(),
                    creado_en=ahora,
                ),
            )
            log.info(
                "envio %s canal=%s poliza_id=%s event_id=%s",
                "registrado" if creado else "repetido (omitido)", canal.value, datos["poliza_id"], evento.event_id,
            )

    # --- 2. entregar ----------------------------------------------------------------------------------------------

    def espera(self, intento: int) -> float:
        return min(self._base_s * 2 ** max(intento - 1, 0), self._maxima_s)

    async def procesar_pendientes(self, limite: int = 10) -> int:
        """Una vuelta: reclama un lote y entrega cada envío. Devuelve cuántos se intentaron."""
        pendientes = await asyncio.to_thread(self._reclamar, limite)
        for pendiente in pendientes:
            try:
                referencia = await self._entregar(pendiente)
            except Exception as error:  # noqa: BLE001 - cualquier fallo del canal se reintenta o deja el envío FALLIDO
                await asyncio.to_thread(self._registrar_fallo, pendiente.id, error)
            else:
                await asyncio.to_thread(self._marcar_enviado, pendiente.id, referencia)
        return len(pendientes)

    async def ejecutar(self, intervalo_s: float = 1.0) -> None:
        while True:
            try:
                if await self.procesar_pendientes() == 0:
                    await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el procesador de notificaciones")
                await asyncio.sleep(intervalo_s * 4)

    def _reclamar(self, limite: int) -> list[_Pendiente]:
        """Anota el intento y reserva el envío hasta el siguiente (otra réplica no lo toma mientras se entrega).

        Si el proceso cae a mitad, la reserva vence y se reintenta; si ya no quedan intentos, pasa a FALLIDO.
        """
        ahora = self._reloj()
        tomados: list[_Pendiente] = []
        with self._fabrica() as s, s.begin():
            filas = s.execute(
                select(EnvioDB)
                .where(
                    EnvioDB.estado == EstadoEnvio.PENDIENTE.value,
                    or_(EnvioDB.proximo_intento_en.is_(None), EnvioDB.proximo_intento_en <= ahora),
                )
                .order_by(EnvioDB.creado_en)
                .limit(limite)
                .with_for_update(skip_locked=True)
            ).scalars()
            for e in filas:
                if e.intentos >= self._max_intentos:
                    self._agotar(e, e.ultimo_error or "SinRespuesta")
                    continue
                e.intentos += 1
                e.proximo_intento_en = ahora + timedelta(seconds=self.espera(e.intentos))
                tomados.append(_Pendiente(e.id, e.canal, e.cliente_id, e.idioma, dict(e.datos)))
        return tomados

    def _agotar(self, e: EnvioDB, error: str) -> None:
        e.estado = EstadoEnvio.FALLIDO.value
        e.proximo_intento_en = None
        e.ultimo_error = error[:100]
        # Sin correo ni mensaje de error: solo ids y el nombre de la clase.
        log.error("envio fallido envio_id=%s canal=%s intentos=%s error=%s", e.id, e.canal, e.intentos, error)

    async def _entregar(self, p: _Pendiente) -> str:
        if p.canal == Canal.PUSH.value:
            return await self._push.entregar(
                plantillas.construir_push(
                    envio_id=p.id, cliente_id=p.cliente_id, suscripcion_id=p.datos["suscripcion_id"], idioma=p.idioma
                )
            )
        contacto = await self._identidad.contacto(p.cliente_id)
        if contacto is None:
            raise ErrorEnvio("ClienteSinContacto")
        enlace = enlace_vence = None
        if self._url_publica:
            emitido = await self._certificados.emitir_enlace(p.datos["poliza_id"], p.idioma)
            if emitido is not None:
                enlace, enlace_vence = f"{self._url_publica}{emitido.ruta}", emitido.vence_en
        correo = plantillas.construir_correo(
            destinatario=contacto.email,
            nombre=contacto.nombre or None,
            idioma=p.idioma,
            numero_poliza=p.datos["numero_poliza"],
            producto=p.datos["producto"],
            vigencia_desde=_fecha(p.datos["vigencia_desde"]),
            vigencia_hasta=_fecha(p.datos["fecha_vencimiento"]),
            enlace=enlace,
            enlace_vence=enlace_vence,
        )
        return await self._correo.enviar(correo)

    def _marcar_enviado(self, envio_id: str, referencia: str) -> None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            e = s.get(EnvioDB, envio_id, with_for_update=True)
            e.estado = EstadoEnvio.ENVIADO.value
            e.enviado_en = ahora
            e.proximo_intento_en = None
            e.ultimo_error = None
            e.referencia_proveedor = referencia[:200]
        log.info("envio entregado envio_id=%s canal=%s", envio_id, e.canal)

    def _registrar_fallo(self, envio_id: str, error: Exception) -> None:
        nombre = type(error).__name__ if not isinstance(error, ErrorEnvio) else (str(error) or "ErrorEnvio")
        with self._fabrica() as s, s.begin():
            e = s.get(EnvioDB, envio_id, with_for_update=True)
            if e is None or e.estado != EstadoEnvio.PENDIENTE.value:
                return
            if e.intentos >= self._max_intentos:
                self._agotar(e, nombre)
                return
            e.ultimo_error = nombre[:100]
            log.warning("no se pudo entregar envio_id=%s canal=%s intento=%s error=%s", e.id, e.canal, e.intentos, nombre)
