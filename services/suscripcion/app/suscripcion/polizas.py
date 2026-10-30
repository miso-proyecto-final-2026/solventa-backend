"""Póliza de una suscripción con el cobro confirmado (HU20): registro, generación del documento y emisión.

Tres pasos, como el cobro de MS Pagos (ADR-0014): un documento no se deshace, así que no puede vivir dentro del manejador que
la plantilla revierte si algo falla.

1. `registrar_poliza` corre en la transacción del consumidor del cobro y solo inserta la fila (número y vigencia). Es barata,
   idempotente y nunca bloquea ni revierte el pago.
2. `GeneradorDocumentos` toma las pólizas pendientes, arma los datos, renderiza el PDF y lo guarda. Reintenta con espera
   creciente y, agotados los intentos, deja la póliza en `FALLIDO` visible para el operador.
3. `emitir_poliza` (la dispara HU21 tras la firma) pasa la póliza a `VIGENTE` y publica `poliza_emitida`.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contexto import traza_id
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.almacen import AlmacenDocumentos
from app.suscripcion.clientes import Cotizaciones, Identidad
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.documento import (
    CoberturaDocumento,
    DatosPoliza,
    Plantilla,
    renderizar_poliza,
)
from app.suscripcion.dominio import EstadoSaga, PasoSaga
from app.suscripcion.errores import PolizaNoEncontrada, PolizaNoLista
from app.suscripcion.modelos_db import PolizaDB, SuscripcionDB

log = logging.getLogger("suscripcion.polizas")

FUENTE = "ms-suscripcion"
# Base32 de Crockford en mayúsculas: sin I, L, O ni U para evitar confusiones al leer o dictar el número.
ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LARGO_NUMERO = 20
INTENTOS_NUMERO = 3

PENDIENTE_FIRMA = "PENDIENTE_FIRMA"
VIGENTE = "VIGENTE"
DOC_PENDIENTE = "PENDIENTE"
DOC_GENERADO = "GENERADO"
DOC_FALLIDO = "FALLIDO"


class NumeroPolizaNoGenerado(RuntimeError):
    """Tres números seguidos chocaron con uno existente: no debería ocurrir con unos 100 bits de aleatoriedad."""


class DatosIncompletos(RuntimeError):
    """Falta algo que el documento necesita (el cliente o las coberturas): se reintenta."""


def generar_numero() -> str:
    """20 caracteres aleatorios del alfabeto de Crockford. No depende de un contador compartido entre réplicas."""
    return "".join(secrets.choice(ALFABETO) for _ in range(LARGO_NUMERO))


def sumar_meses(fecha: datetime, meses: int) -> datetime:
    total = fecha.month - 1 + meses
    anio, mes = fecha.year + total // 12, total % 12 + 1
    return fecha.replace(year=anio, month=mes, day=min(fecha.day, calendar.monthrange(anio, mes)[1]))


def clave_documento(suscripcion_id: str, numero_poliza: str) -> str:
    """Sin datos personales. Un reintento sobrescribe la misma clave, lo que crea una versión nueva en S3."""
    return f"polizas/{suscripcion_id}/{numero_poliza}.pdf"


# --- paso 1: registrar -------------------------------------------------------------------------------------------

def registrar_poliza(
    sesion: Session,
    susc: SuscripcionDB,
    ahora: datetime,
    *,
    vigencia_meses: int = 12,
    plantilla_version: str = "v1",
    generar: Callable[[], str] = generar_numero,
) -> PolizaDB:
    """Crea la póliza de la suscripción, o devuelve la que ya tiene. Corre en la transacción del llamador.

    El número se asigna una sola vez, aquí. Las reentregas del evento y los reintentos del PDF reutilizan la fila
    (`suscripcion_id` es UNIQUE), así que nunca piden otro número. Las pólizas no se borran: ningún número queda libre.
    """
    existente = repo.poliza_de_suscripcion(sesion, susc.id)
    if existente is not None:
        return existente
    desde = repo.utc(susc.cobro_resuelto_en) or ahora
    for _ in range(INTENTOS_NUMERO):
        poliza = PolizaDB(
            id=str(uuid4()),
            suscripcion_id=susc.id,
            cliente_id=susc.cliente_id,
            numero_poliza=generar(),
            estado=PENDIENTE_FIRMA,
            documento_estado=DOC_PENDIENTE,
            documento_intentos=0,
            plantilla_version=plantilla_version,
            vigencia_desde=desde,
            fecha_vencimiento=sumar_meses(desde, vigencia_meses),
            created_at=ahora,
            updated_at=ahora,
        )
        try:
            with sesion.begin_nested():
                sesion.add(poliza)
                sesion.flush()
        except IntegrityError:
            # O ganó otra réplica (la póliza ya existe) o el número chocó con otro: se genera uno nuevo.
            existente = repo.poliza_de_suscripcion(sesion, susc.id)
            if existente is not None:
                return existente
            log.warning("colision de numero de poliza suscripcion_id=%s", susc.id)
            continue
        log.info("poliza registrada suscripcion_id=%s poliza_id=%s", susc.id, poliza.id)
        return poliza
    raise NumeroPolizaNoGenerado(susc.id)


# --- paso 2: generar el documento ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Base:
    """Lo que el generador lee de la base antes de salir a la red (la sesión ya está cerrada)."""

    poliza_id: str
    suscripcion_id: str
    cliente_id: str
    cotizacion_id: str
    numero_poliza: str
    creada_en: datetime
    vigencia_desde: datetime
    vigencia_hasta: datetime
    producto: str
    monto_asegurado: int
    prima_anual: int
    prima_mensual: int
    periodicidad_pago: str
    referencia_cobro: str | None
    coberturas: tuple[CoberturaDocumento, ...]


def _ahora() -> datetime:
    return datetime.now(UTC)


class GeneradorDocumentos:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        almacen: AlmacenDocumentos,
        identidad: Identidad,
        cotizaciones: Cotizaciones,
        plantilla: Plantilla,
        reloj: Callable[[], datetime] = _ahora,
        max_intentos: int = 5,
        espera_base_s: float = 5.0,
        espera_maxima_s: float = 300.0,
    ) -> None:
        self._fabrica = fabrica
        self._almacen = almacen
        self._identidad = identidad
        self._cotizaciones = cotizaciones
        self._plantilla = plantilla
        self._reloj = reloj
        self._max_intentos = max_intentos
        self._base_s = espera_base_s
        self._maxima_s = espera_maxima_s

    def espera(self, intento: int) -> float:
        return min(self._base_s * 2 ** max(intento - 1, 0), self._maxima_s)

    async def procesar_pendientes(self, limite: int = 5) -> int:
        """Una vuelta: reclama un lote y genera cada documento. Devuelve cuántos se intentaron."""
        ids = await asyncio.to_thread(self._reclamar, limite)
        for poliza_id in ids:
            try:
                await self._generar(poliza_id)
            except Exception as error:  # noqa: BLE001 - cualquier fallo se reintenta o se marca FALLIDO
                await asyncio.to_thread(self._registrar_fallo, poliza_id, error)
        return len(ids)

    async def ejecutar(self, intervalo_s: float = 1.0) -> None:
        while True:
            try:
                if await self.procesar_pendientes() == 0:
                    await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el generador de documentos de póliza")
                await asyncio.sleep(intervalo_s * 4)

    def _reclamar(self, limite: int) -> list[str]:
        """Marca el intento y reserva la póliza hasta el próximo (otra réplica no la toma mientras se genera).

        Las filas se bloquean con SKIP LOCKED. Si el proceso cae a mitad de la generación, la reserva vence y se reintenta.
        """
        ahora = self._reloj()
        ids: list[str] = []
        with self._fabrica() as s, s.begin():
            filas = s.execute(
                select(PolizaDB)
                .where(
                    PolizaDB.documento_estado == DOC_PENDIENTE,
                    or_(PolizaDB.documento_proximo_intento.is_(None), PolizaDB.documento_proximo_intento <= ahora),
                )
                .order_by(PolizaDB.created_at)
                .limit(limite)
                .with_for_update(skip_locked=True)
            ).scalars()
            for p in filas:
                if p.documento_intentos >= self._max_intentos:
                    # El proceso cayó durante el último intento: ya no quedan más.
                    self._agotar(p, ahora, p.documento_error or "SinRespuesta")
                    continue
                p.documento_intentos += 1
                p.documento_proximo_intento = ahora + timedelta(seconds=self.espera(p.documento_intentos))
                p.updated_at = ahora
                ids.append(p.id)
        return ids

    def _agotar(self, p: PolizaDB, ahora: datetime, error: str) -> None:
        p.documento_estado = DOC_FALLIDO
        p.documento_proximo_intento = None
        p.documento_error = error[:500]
        p.updated_at = ahora
        # Sin PII: solo identificadores y el nombre del error.
        log.error("documento de poliza fallido poliza_id=%s intentos=%s error=%s", p.id, p.documento_intentos, error)

    def _leer(self, poliza_id: str) -> _Base:
        with self._fabrica() as s:
            p = s.get(PolizaDB, poliza_id)
            susc = s.get(SuscripcionDB, p.suscripcion_id) if p is not None else None
            if p is None or susc is None:
                raise DatosIncompletos("poliza sin suscripcion")
            return _Base(
                poliza_id=p.id,
                suscripcion_id=susc.id,
                cliente_id=p.cliente_id,
                cotizacion_id=susc.cotizacion_id,
                numero_poliza=p.numero_poliza,
                creada_en=repo.utc(p.created_at),
                vigencia_desde=repo.utc(p.vigencia_desde),
                vigencia_hasta=repo.utc(p.fecha_vencimiento),
                producto=susc.producto,
                monto_asegurado=int(susc.monto_asegurado),
                prima_anual=int(susc.prima_anual),
                prima_mensual=int(susc.prima_mensual),
                periodicidad_pago=susc.periodicidad_pago,
                referencia_cobro=susc.cobro_referencia,
                coberturas=tuple(
                    CoberturaDocumento(tipo=c["tipo"], monto=int(c["monto"]), deducible=int(c.get("deducible", 0)))
                    for c in (susc.coberturas or [])
                ),
            )

    async def _generar(self, poliza_id: str) -> None:
        inicio = self._reloj()
        base = await asyncio.to_thread(self._leer, poliza_id)
        cliente = await self._identidad.resumen_cliente(base.cliente_id)
        if cliente is None:
            raise DatosIncompletos("cliente inexistente en Identidad")
        coberturas = base.coberturas
        if not coberturas:
            # Saga anterior a HU20: se relee la cotización aceptada (solo lectura, sin recalcular nada).
            cotizacion = await self._cotizaciones.obtener(base.cotizacion_id)
            coberturas = tuple(CoberturaDocumento(c.tipo, c.monto, c.deducible) for c in (cotizacion.coberturas if cotizacion else ()))
        if not coberturas:
            raise DatosIncompletos("la cotización no tiene coberturas")
        datos = DatosPoliza(
            numero_poliza=base.numero_poliza,
            generada_en=self._reloj(),
            vigencia_desde=base.vigencia_desde,
            vigencia_hasta=base.vigencia_hasta,
            cliente_nombre=f"{cliente.nombre} {cliente.apellido}".strip(),
            cliente_tipo_documento=cliente.tipo_documento,
            cliente_documento_enmascarado=cliente.documento_enmascarado,
            producto=base.producto,
            monto_asegurado=base.monto_asegurado,
            coberturas=coberturas,
            prima_anual=base.prima_anual,
            prima_mensual=base.prima_mensual,
            periodicidad_pago=base.periodicidad_pago,
            referencia_cobro=base.referencia_cobro,
        )
        contenido = await asyncio.to_thread(renderizar_poliza, datos, self._plantilla)
        clave = clave_documento(base.suscripcion_id, base.numero_poliza)
        await asyncio.to_thread(self._almacen.guardar, clave, contenido, "application/pdf")
        generado_en = self._reloj()
        await asyncio.to_thread(self._marcar_generado, poliza_id, clave, generado_en)
        log.info(
            "documento de poliza generado poliza_id=%s bytes=%s duracion_ms=%s desde_el_registro_ms=%s",
            poliza_id, len(contenido), int((generado_en - inicio).total_seconds() * 1000),
            int((generado_en - base.creada_en).total_seconds() * 1000),
        )

    def _marcar_generado(self, poliza_id: str, clave: str, ahora: datetime) -> None:
        with self._fabrica() as s, s.begin():
            p = s.get(PolizaDB, poliza_id, with_for_update=True)
            p.documento_estado = DOC_GENERADO
            p.documento_s3_key = clave
            p.documento_generado_en = ahora
            p.documento_proximo_intento = None
            p.documento_error = None
            p.updated_at = ahora

    def _registrar_fallo(self, poliza_id: str, error: Exception) -> None:
        ahora = self._reloj()
        nombre = type(error).__name__  # nunca el mensaje: puede traer datos del cliente
        with self._fabrica() as s, s.begin():
            p = s.get(PolizaDB, poliza_id, with_for_update=True)
            if p is None or p.documento_estado != DOC_PENDIENTE:
                return
            if p.documento_intentos >= self._max_intentos:
                self._agotar(p, ahora, nombre)
                return
            p.documento_error = nombre
            p.updated_at = ahora
            log.warning(
                "no se pudo generar el documento poliza_id=%s intento=%s error=%s", p.id, p.documento_intentos, nombre
            )


# --- paso 3: emitir (la dispara HU21 tras la firma) -------------------------------------------------------------------

def emitir_poliza(sesion: Session, poliza_id: str, ahora: datetime) -> PolizaDB:
    """Pasa la póliza a `VIGENTE` y la saga a `EMITIDA` / `EMISION`, y encola `poliza_emitida`. Corre en la transacción del llamador.

    Exige el documento generado. Es idempotente: emitir una póliza ya vigente no cambia nada ni repite el evento.
    """
    poliza = sesion.get(PolizaDB, poliza_id, with_for_update=True)
    if poliza is None:
        raise PolizaNoEncontrada
    if poliza.estado == VIGENTE:
        return poliza
    if poliza.documento_estado != DOC_GENERADO:
        raise PolizaNoLista
    susc = sesion.get(SuscripcionDB, poliza.suscripcion_id, with_for_update=True)
    poliza.estado = VIGENTE
    poliza.fecha_emision = ahora
    poliza.updated_at = ahora
    susc.estado_saga = EstadoSaga.EMITIDA.value
    susc.paso_actual = PasoSaga.EMISION.value
    susc.updated_at = ahora

    campos: dict[str, Any] = {}
    if traza_id.get():
        campos["trace_id"] = traza_id.get()
    evento = Evento(
        event_type="poliza_emitida",
        source=FUENTE,
        timestamp=ahora,
        correlation_id=susc.correlacion_id or susc.id,
        payload={
            "poliza_id": poliza.id,
            "suscripcion_id": susc.id,
            "cliente_id": poliza.cliente_id,
            "numero_poliza": poliza.numero_poliza,
            "producto": susc.producto,
            "vigencia_desde": repo.utc(poliza.vigencia_desde).isoformat(),
            "fecha_vencimiento": repo.utc(poliza.fecha_vencimiento).isoformat(),
            "fecha_emision": ahora.isoformat(),
        },
        **campos,
    )
    repo.encolar_evento(
        sesion, event_id=evento.event_id, topic=TOPIC_CICLO_VIDA, clave=poliza.id,
        payload=evento.model_dump(mode="json"), ahora=ahora,
    )
    log.info("poliza emitida poliza_id=%s suscripcion_id=%s", poliza.id, susc.id)
    return poliza
