"""Caso de uso de la saga de suscripción: iniciar (paso 0) y decidir (paso DECISION).

Los cambios de la saga y los eventos que provocan se guardan en la misma transacción (outbox); un proceso aparte los publica.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contexto import correlacion_id
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.clientes import Cotizaciones, Identidad, Perfiles
from app.suscripcion.config import TOPIC_COMANDOS, TOPIC_REVISION
from app.suscripcion.decision import decidir
from app.suscripcion.dominio import Decision, EstadoSaga, PasoSaga, SolicitudDecision
from app.suscripcion.errores import (
    AccesoDenegado,
    ClaveReutilizada,
    CotizacionNoEncontrada,
    CotizacionSinCuenta,
    CotizacionVencida,
    CotizacionYaUsada,
    NoAutenticado,
    PolizaNoEncontrada,
    SolicitudInvalida,
)
from app.suscripcion.modelos_db import PolizaDB, SuscripcionDB
from app.suscripcion.reglas import Reglas

log = logging.getLogger("suscripcion")

FUENTE = "ms-suscripcion"
REDACTADO = "[redactado]"
PERIODICIDADES = ("ANUAL", "MENSUAL")
_PAN_CORRIDO = re.compile(r"\d{13,19}")
_PAN_AGRUPADO = re.compile(r"(?<![0-9A-Za-z])\d{4}([ -])\d{4}\1\d{4}\1\d{1,7}(?![0-9A-Za-z])")
_PAN_AMEX = re.compile(r"(?<![0-9A-Za-z])\d{4}([ -])\d{6}\1\d{5}(?![0-9A-Za-z])")


def _ahora() -> datetime:
    return datetime.now(UTC)


def monto_a_cobrar(susc: SuscripcionDB) -> Decimal:
    """El primer período de la periodicidad elegida: la prima mensual o la anual."""
    return Decimal(susc.prima_mensual if susc.periodicidad_pago == "MENSUAL" else susc.prima_anual)


def clave_cobro(suscripcion_id: str) -> str:
    """Clave de idempotencia del cobro de la prima en MS Pagos: una saga, un cobro."""
    return f"cobro_prima:{suscripcion_id}"


def redactar(sobre: dict[str, Any]) -> dict[str, Any]:
    """Copia del evento sin el token de pago, para conservarlo como evidencia una vez publicado."""
    copia = {**sobre, "payload": {**sobre["payload"]}}
    if "metodo_pago" in copia["payload"]:
        copia["payload"]["metodo_pago"] = REDACTADO
    return copia


def parece_numero_de_tarjeta(valor: str) -> bool:
    """Las formas reales de escribir una tarjeta, también dentro de un texto (SAQ-A, ADR-0014); un `tok_sim_<uuid>` no cae aquí.

    Es la misma regla de `identificador_parece_pan` en MS Pagos, copiada a propósito (ADR-0001): lo que Pagos rechazaría al
    cobrar se rechaza aquí, cuando todavía se le puede responder al asesor.
    """
    return bool(_PAN_CORRIDO.search(valor) or _PAN_AGRUPADO.search(valor) or _PAN_AMEX.search(valor))


class ServicioSuscripcion:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        identidad: Identidad,
        cotizaciones: Cotizaciones,
        perfiles: Perfiles,
        reglas: Reglas,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._identidad = identidad
        self._cotizaciones = cotizaciones
        self._perfiles = perfiles
        self._reglas = reglas
        self._reloj = reloj

    # --- iniciar ------------------------------------------------------------------------------------------------

    async def iniciar(
        self,
        *,
        token: str | None,
        clave: str | None,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str = "ANUAL",
    ) -> tuple[SuscripcionDB, bool]:
        """Devuelve (suscripción, creada). `creada=False` si la clave ya existía: se devuelve la misma sin tocar nada."""
        if not token:
            raise NoAutenticado
        cliente_id = await self._identidad.cliente_de_token(token)
        if cliente_id is None:
            raise NoAutenticado
        return await self.iniciar_como(
            cliente_id,
            clave=clave,
            cotizacion_id=cotizacion_id,
            metodo_pago=metodo_pago,
            acepta_terminos=acepta_terminos,
            periodicidad_pago=periodicidad_pago,
        )

    async def iniciar_asistida(
        self,
        asesor_id: str,
        *,
        clave: str | None,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str = "ANUAL",
    ) -> tuple[SuscripcionDB, bool]:
        """HU39: el asesor inicia la saga de su propia cotización, con el cliente presente. El titular es el de la cotización."""
        cotizacion = await self._cotizaciones.obtener(cotizacion_id)
        if cotizacion is None:
            raise CotizacionNoEncontrada
        if cotizacion.asesor_id != asesor_id:
            raise AccesoDenegado
        if cotizacion.sin_cuenta:
            raise CotizacionSinCuenta
        if parece_numero_de_tarjeta(metodo_pago):
            raise SolicitudInvalida(detalles=[{"campo": "metodo_pago", "motivo": "debe ser el token de la pasarela, nunca el número de tarjeta"}])
        return await self.iniciar_como(
            cotizacion.cliente_id,
            clave=clave,
            cotizacion_id=cotizacion_id,
            metodo_pago=metodo_pago,
            acepta_terminos=acepta_terminos,
            periodicidad_pago=periodicidad_pago,
        )

    async def iniciar_como(
        self,
        cliente_id: str,
        *,
        clave: str | None,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str = "ANUAL",
    ) -> tuple[SuscripcionDB, bool]:
        """Inicia la saga para un cliente ya identificado."""
        if not clave or not 1 <= len(clave) <= 100:
            raise SolicitudInvalida(detalles=[{"campo": "Idempotency-Key", "motivo": "es obligatoria (1 a 100 caracteres)"}])
        if not acepta_terminos:
            raise SolicitudInvalida(detalles=[{"campo": "acepta_terminos", "motivo": "debe ser true"}])
        if periodicidad_pago not in PERIODICIDADES:
            raise SolicitudInvalida(detalles=[{"campo": "periodicidad_pago", "motivo": "debe ser ANUAL o MENSUAL"}])

        existente = await asyncio.to_thread(self._por_clave, cliente_id, clave)
        if existente is not None:
            return self._verificar_repeticion(existente, cotizacion_id), False

        cotizacion = await self._cotizaciones.obtener(cotizacion_id)
        # Una cotización ajena se informa como inexistente: no se revela que existe.
        if cotizacion is None or cotizacion.cliente_id != cliente_id:
            raise CotizacionNoEncontrada
        ahora = self._reloj()
        if cotizacion.vigente_hasta <= ahora:
            raise CotizacionVencida

        try:
            creada = await asyncio.to_thread(
                self._crear, cliente_id, clave, cotizacion, metodo_pago, periodicidad_pago, ahora
            )
        except IntegrityError:
            creada = None
        if creada is not None:
            return creada, True
        # Otra réplica ganó la carrera. Si fue con esta misma clave, es una repetición; si no, la cotización ya está en uso.
        existente = await asyncio.to_thread(self._por_clave, cliente_id, clave)
        if existente is None:
            raise CotizacionYaUsada
        return self._verificar_repeticion(existente, cotizacion_id), False

    @staticmethod
    def _verificar_repeticion(existente: SuscripcionDB, cotizacion_id: str) -> SuscripcionDB:
        if existente.cotizacion_id != cotizacion_id:
            raise ClaveReutilizada
        return existente

    def _por_clave(self, cliente_id: str, clave: str) -> SuscripcionDB | None:
        with self._fabrica() as s:
            return repo.buscar_por_clave(s, cliente_id, clave)

    def _crear(
        self, cliente_id: str, clave: str, c: Any, metodo_pago: str, periodicidad_pago: str, ahora: datetime
    ) -> SuscripcionDB | None:
        with self._fabrica() as s, s.begin():
            if repo.buscar_activa_por_cotizacion(s, c.id) is not None:
                return None
            return repo.nueva_suscripcion(
                s,
                id=str(uuid4()),
                cotizacion_id=c.id,
                cliente_id=cliente_id,
                clave_idempotencia=clave,
                producto=c.producto,
                moneda=c.moneda,
                monto_asegurado=c.monto_asegurado,
                prima_mensual=c.prima_mensual,
                prima_anual=c.prima_anual,
                ahora=ahora,
                correlacion_id=correlacion_id.get(),
                metodo_pago_token=metodo_pago,
                periodicidad_pago=periodicidad_pago,
                coberturas=[{"tipo": x.tipo, "monto": x.monto, "deducible": x.deducible} for x in c.coberturas] or None,
                cotizacion_version_reglas=c.version_reglas,
                asesor_id=c.asesor_id,
            )

    # --- consultar ----------------------------------------------------------------------------------------------

    async def consultar(self, suscripcion_id: str, token: str | None) -> SuscripcionDB:
        from app.suscripcion.errores import SuscripcionNoEncontrada

        if not token:
            raise NoAutenticado
        cliente_id = await self._identidad.cliente_de_token(token)
        if cliente_id is None:
            raise NoAutenticado

        def leer() -> SuscripcionDB | None:
            with self._fabrica() as s:
                return repo.obtener(s, suscripcion_id)

        susc = await asyncio.to_thread(leer)
        if susc is None or susc.cliente_id != cliente_id:
            raise SuscripcionNoEncontrada
        return susc

    def poliza_de_suscripcion(self, suscripcion_id: str) -> PolizaDB | None:
        with self._fabrica() as s:
            return repo.poliza_de_suscripcion(s, suscripcion_id)

    async def _cliente_del_token(self, token: str | None) -> str:
        if not token:
            raise NoAutenticado
        cliente_id = await self._identidad.cliente_de_token(token)
        if cliente_id is None:
            raise NoAutenticado
        return cliente_id

    async def consultar_poliza(self, suscripcion_id: str, token: str | None) -> tuple[PolizaDB, SuscripcionDB]:
        """La póliza de una suscripción. La de otro cliente responde igual que una inexistente."""
        cliente_id = await self._cliente_del_token(token)

        def leer() -> tuple[PolizaDB, SuscripcionDB] | None:
            with self._fabrica() as s:
                susc = repo.obtener(s, suscripcion_id)
                poliza = repo.poliza_de_suscripcion(s, suscripcion_id)
                return (poliza, susc) if susc is not None and poliza is not None else None

        encontrada = await asyncio.to_thread(leer)
        if encontrada is None or encontrada[1].cliente_id != cliente_id:
            raise PolizaNoEncontrada
        return encontrada

    async def billetera(self, cliente_id: str, token: str | None) -> list[tuple[PolizaDB, SuscripcionDB]]:
        """Las pólizas del cliente. Un `cliente_id` distinto al del token es 403."""
        if await self._cliente_del_token(token) != cliente_id:
            raise AccesoDenegado

        def leer() -> list[tuple[PolizaDB, SuscripcionDB]]:
            with self._fabrica() as s:
                return repo.polizas_de_cliente(s, cliente_id)

        return await asyncio.to_thread(leer)

    def revisiones(self, suscripcion_id: str) -> list[Any]:
        with self._fabrica() as s:
            return repo.revisiones_de(s, suscripcion_id)

    # --- decidir ------------------------------------------------------------------------------------------------

    async def decidir(self, suscripcion_id: str) -> SuscripcionDB | None:
        """Paso DECISION. Seguro de repetir: si la saga ya se decidió no hace nada."""
        def leer() -> SuscripcionDB | None:
            with self._fabrica() as s:
                susc = repo.obtener(s, suscripcion_id)
                return susc if susc is not None and susc.decision is None else None

        susc = await asyncio.to_thread(leer)
        if susc is None:
            return None
        perfil = await self._perfiles.perfil_de(susc.cliente_id)
        resultado = decidir(
            SolicitudDecision(producto=susc.producto, monto_asegurado=int(susc.monto_asegurado), perfil=perfil), self._reglas
        )
        return await asyncio.to_thread(self._guardar_decision, suscripcion_id, resultado)

    def _guardar_decision(self, suscripcion_id: str, resultado: Any) -> SuscripcionDB | None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id, with_for_update=True)
            if susc is None or susc.decision is not None:
                return None  # otra réplica ya decidió
            estado, paso = {
                Decision.APROBADA: (EstadoSaga.APROBADA, PasoSaga.COBRO),
                Decision.RECHAZADA: (EstadoSaga.RECHAZADA, PasoSaga.DECISION),
                # AJUSTADA espera la aceptación del cliente y DERIVADA la de una persona: la saga sigue en la decisión.
                Decision.AJUSTADA: (EstadoSaga.EN_PROGRESO, PasoSaga.DECISION),
                Decision.DERIVADA: (EstadoSaga.EN_PROGRESO, PasoSaga.DECISION),
            }[resultado.decision]
            repo.registrar_decision(
                s,
                susc,
                decision=resultado.decision.value,
                estado_saga=estado.value,
                paso_actual=paso.value,
                motivos=resultado.motivos,
                ajuste=resultado.ajuste,
                reglas_version=resultado.reglas_version,
                ahora=ahora,
            )
            if resultado.decision is Decision.APROBADA:
                self._encolar(
                    s, susc, "cobro_prima_solicitado", TOPIC_COMANDOS, ahora,
                    {
                        "moneda": susc.moneda,
                        # Se conservan las dos primas por compatibilidad con consumidores de HU15.
                        "prima_mensual": int(susc.prima_mensual),
                        "prima_anual": int(susc.prima_anual),
                        # HU18: lo que se cobra (el primer período de la periodicidad elegida) y la clave del cobro.
                        "periodicidad_pago": susc.periodicidad_pago,
                        "monto": f"{monto_a_cobrar(susc):.2f}",
                        "idempotency_key": clave_cobro(susc.id),
                        "metodo_pago": susc.metodo_pago_token,
                    },
                )
                susc.metodo_pago_token = None  # el token solo viaja en el evento y se redacta al publicarlo
            elif resultado.decision is Decision.RECHAZADA:
                susc.metodo_pago_token = None
            elif resultado.decision is Decision.DERIVADA:
                motivo = "; ".join(resultado.motivos)
                repo.crear_revision_asistida(s, susc.id, motivo, ahora)
                self._encolar(s, susc, "revision_asistida_solicitada", TOPIC_REVISION, ahora, {"motivos": resultado.motivos})
            duracion = susc.duracion_decision_ms
        log.info(
            "decision suscripcion_id=%s decision=%s duracion_decision_ms=%s reglas=%s",
            suscripcion_id, resultado.decision.value, duracion, resultado.reglas_version,
        )
        return susc

    @staticmethod
    def _encolar(s: Session, susc: SuscripcionDB, tipo: str, topic: str, ahora: datetime, extra: dict[str, Any]) -> None:
        evento = Evento(
            event_type=tipo,
            source=FUENTE,
            timestamp=ahora,
            correlation_id=susc.correlacion_id or susc.id,
            payload={
                "suscripcion_id": susc.id,
                "cotizacion_id": susc.cotizacion_id,
                "cliente_id": susc.cliente_id,
                "decision": susc.decision,
                **extra,
            },
        )
        repo.encolar_evento(
            s, event_id=evento.event_id, topic=topic, clave=susc.id,
            payload=evento.model_dump(mode="json"), ahora=ahora,
        )

    # --- barrido ------------------------------------------------------------------------------------------------

    async def retomar_pendientes(self, espera_s: float, limite: int = 20) -> int:
        """Decide las sagas que quedaron sin decidir porque el proceso que las iba a decidir cayó."""
        from datetime import timedelta

        def ids() -> list[str]:
            with self._fabrica() as s, s.begin():
                return [x.id for x in repo.pendientes_de_decision(s, antes_de=self._reloj() - timedelta(seconds=espera_s), limite=limite)]

        pendientes = await asyncio.to_thread(ids)
        for i in pendientes:
            await self.decidir(i)
        return len(pendientes)
