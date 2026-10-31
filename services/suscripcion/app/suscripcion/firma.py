"""Paso FIRMA de la saga (HU21): revisar, firmar con autenticación reforzada, guardar la evidencia y emitir.

Tres momentos, cada uno en su transacción (un PDF en S3 no se deshace, como en HU20):

1. Validar y autenticar al firmante, y guardar la copia firmada en el almacén (sin transacción abierta).
2. Registrar la firma: evidencia inmutable y saga en `FIRMADA` / `EMISION`. Una restricción única por suscripción hace que
   firmar dos veces no cree dos firmas, ni siquiera desde dos réplicas.
3. Emitir la póliza (`emitir_poliza`, HU20). Si el proceso cae entre 2 y 3, el barrido `emitir_firmadas` la emite.

Si la firma vence o falla (demasiados códigos erróneos), la saga compensa el cobro: pide a MS Pagos el reembolso con el
evento `reembolso_cobro_solicitado` y queda `COMPENSADA` cuando MS Pagos confirma (ver `cobros.py`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.almacen import AlmacenDocumentos
from app.suscripcion.clientes import Identidad
from app.suscripcion.config import TOPIC_COMANDOS
from app.suscripcion.constancia import EvidenciaFirma, sellar_documento
from app.suscripcion.dominio import EstadoSaga, PasoSaga
from app.suscripcion.errores import (
    AutenticacionInvalida,
    DocumentoAlterado,
    FirmaFallida,
    FirmaNoDisponible,
    FirmaNoEncontrada,
    FirmaVencida,
    NoAutenticado,
    PolizaNoEncontrada,
    PolizaNoLista,
    SolicitudInvalida,
    SuscripcionNoEncontrada,
)
from app.suscripcion.modelos_db import FirmaDB, PolizaDB, SuscripcionDB
from app.suscripcion.polizas import DOC_GENERADO, emitir_poliza
from app.suscripcion.proveedor_firma import ProveedorFirma
from app.suscripcion.reglas import Reglas
from app.suscripcion.servicio import FUENTE, monto_a_cobrar

log = logging.getLogger("suscripcion.firma")

CANALES = ("MOVIL", "WEB")
MODALIDAD_CLIENTE = "CLIENTE"
MODALIDAD_ASISTIDA_PANTALLA = "ASISTIDA_PANTALLA"
MOTIVO_VENCIDA = "FIRMA_VENCIDA"
MOTIVO_FALLIDA = "FIRMA_FALLIDA"
COMPENSACION_SOLICITADA = "SOLICITADA"
COMPENSACION_COMPLETADA = "COMPLETADA"
POLIZA_ANULADA = "ANULADA"
COMANDO_REEMBOLSO = "reembolso_cobro_solicitado"


def clave_reembolso(suscripcion_id: str) -> str:
    """Clave de idempotencia del reembolso en MS Pagos: una saga, un reembolso."""
    return f"reembolso_prima:{suscripcion_id}"


def clave_copia_firmada(suscripcion_id: str, numero_poliza: str) -> str:
    """Sin datos personales. Un reintento sobrescribe la misma clave, lo que crea una versión nueva en S3."""
    return f"firmadas/{suscripcion_id}/{numero_poliza}.pdf"


def _ahora() -> datetime:
    return datetime.now(UTC)


def _sha256(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


@dataclass(frozen=True)
class DocumentoParaFirmar:
    suscripcion_id: str
    numero_poliza: str
    documento_sha256: str
    vence_en: datetime | None
    metodo_autenticacion: str
    estado: str  # PENDIENTE | FIRMADA


@dataclass(frozen=True)
class OrigenFirma:
    """Desde dónde firmó el cliente: parte de la evidencia de no repudio."""

    canal: str
    ip: str | None
    dispositivo: str | None
    modalidad: str = MODALIDAD_CLIENTE
    asesor_id: str | None = None


def firma_gestionable(susc: SuscripcionDB, poliza: PolizaDB | None, vence_en: datetime | None, ahora: datetime) -> bool:
    """Si se puede gestionar la firma ahora (HU39). Con un reembolso pedido la saga sigue en `COBRO_REALIZADO`: no basta el estado."""
    return (
        (susc.estado_saga, susc.paso_actual) == (EstadoSaga.COBRO_REALIZADO.value, PasoSaga.FIRMA.value)
        and susc.compensacion_estado is None
        and poliza is not None
        and poliza.documento_estado == DOC_GENERADO
        and (vence_en is None or ahora < vence_en)
    )


def encolar_reembolso(sesion: Session, susc: SuscripcionDB, ahora: datetime) -> None:
    evento = Evento(
        event_type=COMANDO_REEMBOLSO,
        source=FUENTE,
        timestamp=ahora,
        correlation_id=susc.correlacion_id or susc.id,
        payload={
            "suscripcion_id": susc.id,
            "cliente_id": susc.cliente_id,
            "cobro_transaccion_id": susc.cobro_transaccion_id,
            "monto": f"{monto_a_cobrar(susc):.2f}",
            "moneda": susc.moneda,
            "motivo": susc.compensacion_motivo,
            "idempotency_key": clave_reembolso(susc.id),
        },
    )
    repo.encolar_evento(
        sesion, event_id=evento.event_id, topic=TOPIC_COMANDOS, clave=susc.id,
        payload=evento.model_dump(mode="json"), ahora=ahora,
    )


def compensar(sesion: Session, susc: SuscripcionDB, motivo: str, ahora: datetime) -> bool:
    """Pide el reembolso del cobro. Idempotente: la segunda llamada no hace nada. Corre en la transacción del llamador."""
    if susc.compensacion_estado is not None:
        return False
    susc.compensacion_estado = COMPENSACION_SOLICITADA
    susc.compensacion_motivo = motivo
    susc.compensacion_solicitada_en = ahora
    susc.updated_at = ahora
    encolar_reembolso(sesion, susc, ahora)
    log.info("compensacion solicitada suscripcion_id=%s motivo=%s", susc.id, motivo)
    return True


def completar_compensacion(sesion: Session, susc: SuscripcionDB, ahora: datetime) -> None:
    """MS Pagos confirmó el reembolso: la saga termina `COMPENSADA` y la póliza queda anulada."""
    susc.estado_saga = EstadoSaga.COMPENSADA.value
    susc.compensacion_estado = COMPENSACION_COMPLETADA
    susc.updated_at = ahora
    poliza = repo.poliza_de_suscripcion(sesion, susc.id)
    if poliza is not None:
        poliza.estado = POLIZA_ANULADA
        poliza.updated_at = ahora


class ServicioFirma:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        identidad: Identidad,
        almacen: AlmacenDocumentos,
        proveedor: ProveedorFirma,
        reglas: Reglas,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._identidad = identidad
        self._almacen = almacen
        self._proveedor = proveedor
        self._reglas = reglas
        self._reloj = reloj

    # --- acceso -------------------------------------------------------------------------------------------------

    async def _cliente_del_token(self, token: str | None) -> str:
        if not token:
            raise NoAutenticado
        cliente_id = await self._identidad.cliente_de_token(token)
        if cliente_id is None:
            raise NoAutenticado
        return cliente_id

    def _cargar(self, suscripcion_id: str, cliente_id: str) -> tuple[SuscripcionDB, PolizaDB]:
        """La suscripción y su póliza. Las de otro cliente responden como inexistentes."""
        with self._fabrica() as s:
            susc = repo.obtener(s, suscripcion_id)
            if susc is None or susc.cliente_id != cliente_id:
                raise SuscripcionNoEncontrada
            poliza = repo.poliza_de_suscripcion(s, suscripcion_id)
            if poliza is None:
                raise PolizaNoEncontrada
            return susc, poliza

    def vence_en(self, susc: SuscripcionDB) -> datetime | None:
        """Cuándo vence la firma. Las sagas anteriores a HU21 no lo guardaron: se deriva del cobro."""
        guardado = repo.utc(susc.firma_vence_en)
        if guardado is not None:
            return guardado
        cobrado = repo.utc(susc.cobro_resuelto_en)
        return None if cobrado is None else cobrado + timedelta(hours=self._reglas.firma_vigencia_horas)

    def _esta_firmada(self, susc: SuscripcionDB) -> bool:
        return susc.estado_saga in (EstadoSaga.FIRMADA.value, EstadoSaga.EMITIDA.value)

    def _verificar_abierta(self, susc: SuscripcionDB) -> None:
        """La firma solo se acepta en el paso FIRMA, sin compensación en curso y antes del vencimiento."""
        if susc.compensacion_estado is not None:
            raise FirmaFallida if susc.compensacion_motivo == MOTIVO_FALLIDA else FirmaVencida
        if (susc.estado_saga, susc.paso_actual) != (EstadoSaga.COBRO_REALIZADO.value, PasoSaga.FIRMA.value):
            raise FirmaNoDisponible
        vence = self.vence_en(susc)
        if vence is not None and self._reloj() >= vence:
            self._compensar_en_transaccion(susc.id, MOTIVO_VENCIDA)
            raise FirmaVencida

    def _compensar_en_transaccion(self, suscripcion_id: str, motivo: str) -> None:
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id, with_for_update=True)
            if susc is not None and (susc.estado_saga, susc.paso_actual) == (
                EstadoSaga.COBRO_REALIZADO.value, PasoSaga.FIRMA.value
            ):
                compensar(s, susc, motivo, self._reloj())

    # --- revisar --------------------------------------------------------------------------------------------------

    async def documento_para_firmar(self, suscripcion_id: str, token: str | None) -> DocumentoParaFirmar:
        return await self.documento_para_firmar_como(suscripcion_id, await self._cliente_del_token(token))

    async def documento_para_firmar_como(self, suscripcion_id: str, cliente_id: str) -> DocumentoParaFirmar:
        susc, poliza = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)
        if self._esta_firmada(susc):
            estado = "FIRMADA"
        else:
            await asyncio.to_thread(self._verificar_abierta, susc)
            estado = "PENDIENTE"
        poliza = await asyncio.to_thread(self._con_hash, poliza)
        return DocumentoParaFirmar(
            suscripcion_id=susc.id,
            numero_poliza=poliza.numero_poliza,
            documento_sha256=poliza.documento_sha256,
            vence_en=self.vence_en(susc),
            metodo_autenticacion=self._proveedor.metodo_autenticacion,
            estado=estado,
        )

    def _con_hash(self, poliza: PolizaDB) -> PolizaDB:
        """Exige el documento generado y le garantiza el hash (las pólizas anteriores a HU21 se calculan una vez)."""
        if poliza.documento_estado != DOC_GENERADO or not poliza.documento_s3_key:
            raise PolizaNoLista
        if poliza.documento_sha256:
            return poliza
        huella = _sha256(self._almacen.leer(poliza.documento_s3_key))
        with self._fabrica() as s, s.begin():
            fila = s.get(PolizaDB, poliza.id, with_for_update=True)
            fila.documento_sha256 = fila.documento_sha256 or huella
            return fila

    async def contenido_documento(self, suscripcion_id: str, token: str | None) -> bytes:
        """El PDF que se va a firmar."""
        return await self.contenido_documento_como(suscripcion_id, await self._cliente_del_token(token))

    async def contenido_documento_como(self, suscripcion_id: str, cliente_id: str) -> bytes:
        _, poliza = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)
        poliza = await asyncio.to_thread(self._con_hash, poliza)
        return await asyncio.to_thread(self._almacen.leer, poliza.documento_s3_key)

    # --- firmar ---------------------------------------------------------------------------------------------------

    async def solicitar_codigo(self, suscripcion_id: str, token: str | None) -> None:
        await self.solicitar_codigo_como(suscripcion_id, await self._cliente_del_token(token))

    async def solicitar_codigo_como(self, suscripcion_id: str, cliente_id: str) -> None:
        susc, _ = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)
        await asyncio.to_thread(self._verificar_abierta, susc)
        await self._proveedor.solicitar_desafio(cliente_id)

    async def firmar(
        self,
        suscripcion_id: str,
        token: str | None,
        *,
        documento_sha256: str,
        documento_revisado: bool,
        codigo: str,
        origen: OrigenFirma,
    ) -> tuple[FirmaDB, bool]:
        """Devuelve (firma, creada). `creada=False`: ya estaba firmada y se devuelve la misma firma, sin tocar nada."""
        return await self.firmar_como(
            suscripcion_id,
            await self._cliente_del_token(token),
            documento_sha256=documento_sha256,
            documento_revisado=documento_revisado,
            codigo=codigo,
            origen=origen,
        )

    async def firmar_como(
        self,
        suscripcion_id: str,
        cliente_id: str,
        *,
        documento_sha256: str,
        documento_revisado: bool,
        codigo: str,
        origen: OrigenFirma,
    ) -> tuple[FirmaDB, bool]:
        """Firma por un cliente ya identificado: el firmante siempre es el cliente, aunque un asesor lo asista (HU39)."""
        if origen.canal not in CANALES:
            raise SolicitudInvalida(detalles=[{"campo": "canal", "motivo": "debe ser MOVIL o WEB"}])
        susc, poliza = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)

        if self._esta_firmada(susc):
            previa = await asyncio.to_thread(self._firma_de, susc.id)
            if previa is None:
                raise FirmaNoDisponible
            if documento_sha256 != previa.documento_sha256:
                raise DocumentoAlterado
            await self._emitir_si_falta(susc.id)
            return previa, False

        await asyncio.to_thread(self._verificar_abierta, susc)
        if not documento_revisado:
            raise SolicitudInvalida(detalles=[{"campo": "documento_revisado", "motivo": "debe confirmar que revisó el documento"}])
        poliza = await asyncio.to_thread(self._con_hash, poliza)
        if documento_sha256 != poliza.documento_sha256:
            raise DocumentoAlterado

        if not await self._proveedor.verificar_desafio(cliente_id, codigo):
            await self._rechazar_codigo(susc.id)
        # El documento guardado debe seguir siendo el que se le mostró al cliente.
        original = await asyncio.to_thread(self._almacen.leer, poliza.documento_s3_key)
        if _sha256(original) != documento_sha256:
            log.error("el documento guardado no coincide con su hash poliza_id=%s", poliza.id)
            raise DocumentoAlterado

        firmada_en = self._reloj()
        referencia = await self._proveedor.registrar_firma(cliente_id, documento_sha256)
        clave = clave_copia_firmada(susc.id, poliza.numero_poliza)
        copia = await asyncio.to_thread(
            sellar_documento,
            original,
            EvidenciaFirma(
                numero_poliza=poliza.numero_poliza,
                firmante_id=cliente_id,
                firmada_en=firmada_en,
                canal=origen.canal,
                ip=origen.ip,
                dispositivo=origen.dispositivo,
                metodo_autenticacion=self._proveedor.metodo_autenticacion,
                proveedor=self._proveedor.nombre,
                referencia_proveedor=referencia,
                documento_sha256=documento_sha256,
                asesor_id=origen.asesor_id,
            ),
        )
        await asyncio.to_thread(self._almacen.guardar, clave, copia, "application/pdf")
        firma, creada = await asyncio.to_thread(
            self._registrar, susc.id, poliza, cliente_id, documento_sha256, firmada_en, origen, referencia, clave
        )
        await self._emitir_si_falta(susc.id)
        return firma, creada

    async def _rechazar_codigo(self, suscripcion_id: str) -> None:
        """Cuenta el código erróneo. Al agotar los intentos la firma se da por fallida y se compensa el cobro."""
        restantes = await asyncio.to_thread(self._contar_fallo, suscripcion_id)
        if restantes <= 0:
            raise FirmaFallida
        raise AutenticacionInvalida(detalles=[{"campo": "codigo", "motivo": f"intentos restantes: {restantes}"}])

    def _contar_fallo(self, suscripcion_id: str) -> int:
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id, with_for_update=True)
            susc.firma_intentos_fallidos += 1
            susc.updated_at = self._reloj()
            restantes = self._reglas.firma_max_intentos - susc.firma_intentos_fallidos
            if restantes <= 0:
                compensar(s, susc, MOTIVO_FALLIDA, self._reloj())
            return restantes

    def _registrar(
        self,
        suscripcion_id: str,
        poliza: PolizaDB,
        cliente_id: str,
        huella: str,
        firmada_en: datetime,
        origen: OrigenFirma,
        referencia: str,
        clave_copia: str,
    ) -> tuple[FirmaDB, bool]:
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id, with_for_update=True)
            previa = s.execute(select(FirmaDB).where(FirmaDB.suscripcion_id == suscripcion_id)).scalar_one_or_none()
            if previa is not None:
                return previa, False  # otra réplica firmó primero
            if susc.compensacion_estado is not None:
                raise FirmaVencida  # venció mientras se firmaba: el reembolso ya está pedido
            ahora = self._reloj()
            firma = FirmaDB(
                id=str(uuid4()),
                suscripcion_id=suscripcion_id,
                poliza_id=poliza.id,
                firmante_id=cliente_id,
                documento_sha256=huella,
                firmada_en=firmada_en,
                canal=origen.canal,
                ip=(origen.ip or None) and origen.ip[:45],
                dispositivo=(origen.dispositivo or None) and origen.dispositivo[:200],
                metodo_autenticacion=self._proveedor.metodo_autenticacion,
                proveedor=self._proveedor.nombre,
                referencia_proveedor=referencia,
                copia_firmada_s3_key=clave_copia,
                modalidad=origen.modalidad,
                asesor_id=origen.asesor_id,
                created_at=ahora,
            )
            try:
                with s.begin_nested():
                    s.add(firma)
                    s.flush()
            except IntegrityError:
                # La restricción única decide si dos réplicas firman a la vez: la perdedora devuelve la firma ganadora.
                ganadora = s.execute(select(FirmaDB).where(FirmaDB.suscripcion_id == suscripcion_id)).scalar_one()
                return ganadora, False
            susc.estado_saga = EstadoSaga.FIRMADA.value
            susc.paso_actual = PasoSaga.EMISION.value
            susc.updated_at = ahora
            log.info("firma registrada suscripcion_id=%s canal=%s", suscripcion_id, origen.canal)
            return firma, True

    def _firma_de(self, suscripcion_id: str) -> FirmaDB | None:
        with self._fabrica() as s:
            return s.execute(select(FirmaDB).where(FirmaDB.suscripcion_id == suscripcion_id)).scalar_one_or_none()

    # --- emitir ---------------------------------------------------------------------------------------------------

    async def _emitir_si_falta(self, suscripcion_id: str) -> None:
        await asyncio.to_thread(self._emitir, suscripcion_id)

    def _emitir(self, suscripcion_id: str) -> bool:
        with self._fabrica() as s, s.begin():
            # Se reclama la emisión con un UPDATE condicionado al estado: de varias réplicas a la vez solo una lo logra,
            # sin depender de que la base soporte bloqueos de fila. Si emitir falla, la transacción deshace el reclamo.
            reclamada = s.execute(
                update(SuscripcionDB)
                .where(SuscripcionDB.id == suscripcion_id, SuscripcionDB.estado_saga == EstadoSaga.FIRMADA.value)
                .values(estado_saga=EstadoSaga.EMITIDA.value)
            ).rowcount
            if not reclamada:
                return False
            poliza = repo.poliza_de_suscripcion(s, suscripcion_id)
            emitir_poliza(s, poliza.id, self._reloj())
            return True

    async def emitir_firmadas(self, limite: int = 20) -> int:
        """Barrido: emite las sagas que quedaron `FIRMADA` porque el proceso cayó antes de emitir."""

        def ids() -> list[str]:
            with self._fabrica() as s:
                return list(
                    s.execute(
                        select(SuscripcionDB.id).where(SuscripcionDB.estado_saga == EstadoSaga.FIRMADA.value).limit(limite)
                    ).scalars()
                )

        emitidas = 0
        for i in await asyncio.to_thread(ids):
            try:
                emitidas += int(await asyncio.to_thread(self._emitir, i))
            except Exception:
                log.exception("no se pudo emitir la póliza suscripcion_id=%s", i)
        return emitidas

    # --- descargar la copia firmada ---------------------------------------------------------------------------------

    async def copia_firmada(self, suscripcion_id: str, token: str | None) -> tuple[bytes, str]:
        """(PDF firmado, número de póliza)."""
        return await self.copia_firmada_como(suscripcion_id, await self._cliente_del_token(token))

    async def copia_firmada_como(self, suscripcion_id: str, cliente_id: str) -> tuple[bytes, str]:
        susc, poliza = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)
        firma = await asyncio.to_thread(self._firma_de, susc.id)
        if firma is None:
            raise FirmaNoEncontrada
        contenido = await asyncio.to_thread(self._almacen.leer, firma.copia_firmada_s3_key)
        return contenido, poliza.numero_poliza

    async def estado_saga(self, suscripcion_id: str) -> str:
        def leer() -> str:
            with self._fabrica() as s:
                return repo.obtener(s, suscripcion_id).estado_saga

        return await asyncio.to_thread(leer)

    async def firma_de(self, suscripcion_id: str, token: str | None) -> FirmaDB:
        cliente_id = await self._cliente_del_token(token)
        susc, _ = await asyncio.to_thread(self._cargar, suscripcion_id, cliente_id)
        firma = await asyncio.to_thread(self._firma_de, susc.id)
        if firma is None:
            raise FirmaNoEncontrada
        return firma

    # --- vencimiento -----------------------------------------------------------------------------------------------

    async def compensar_vencidas(self, limite: int = 20) -> int:
        """Barrido: las firmas cuyo plazo terminó sin que el cliente firmara se compensan solas."""

        def vencidas() -> list[str]:
            ahora = self._reloj()
            with self._fabrica() as s:
                candidatas = s.execute(
                    select(SuscripcionDB)
                    .where(
                        SuscripcionDB.estado_saga == EstadoSaga.COBRO_REALIZADO.value,
                        SuscripcionDB.paso_actual == PasoSaga.FIRMA.value,
                        SuscripcionDB.compensacion_estado.is_(None),
                    )
                    .order_by(SuscripcionDB.created_at)
                    .limit(limite * 5)
                ).scalars()
                return [x.id for x in candidatas if (v := self.vence_en(x)) is not None and ahora >= v][:limite]

        ids = await asyncio.to_thread(vencidas)
        for i in ids:
            await asyncio.to_thread(self._compensar_en_transaccion, i, MOTIVO_VENCIDA)
        return len(ids)

    async def ejecutar(self, intervalo_s: float = 30.0) -> None:
        while True:
            try:
                await self.compensar_vencidas()
                await self.emitir_firmadas()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el barrido de firmas")
            await asyncio.sleep(intervalo_s)

