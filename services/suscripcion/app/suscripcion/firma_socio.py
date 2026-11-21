"""Venta embebida del socio (HU33): lo que un socio hace sobre la saga de las suscripciones que inició.

No repite reglas: inicia con `ServicioSuscripcion` y firma con `ServicioFirma`, los mismos caminos del cliente y del asesor.
Lo que agrega es quién puede (solo el socio que inició la saga) y el registro de cada acción, también de las rechazadas.
Una suscripción de otro socio, o que no es de ningún socio, responde 404: no se revela que existe (criterio 10).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.suscripcion import repositorio as repo
from app.suscripcion.errores import SuscripcionNoEncontrada
from app.suscripcion.firma import (
    MODALIDAD_SOCIO,
    DocumentoParaFirmar,
    OrigenFirma,
    ServicioFirma,
)
from app.suscripcion.modelos_db import (
    AuditoriaSocioDB,
    FirmaDB,
    PolizaDB,
    SuscripcionDB,
)
from app.suscripcion.servicio import ServicioSuscripcion

log = logging.getLogger("suscripcion.socios")

EXITO = "EXITO"
ERROR_INTERNO = "ERROR_INTERNO"
INICIAR_SUSCRIPCION = "INICIAR_SUSCRIPCION"
DESCARGAR_DOCUMENTO = "DESCARGAR_DOCUMENTO"
SOLICITAR_CODIGO = "SOLICITAR_CODIGO"
FIRMAR = "FIRMAR"


def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SuscripcionDeSocio:
    suscripcion: SuscripcionDB
    poliza: PolizaDB | None


class ServicioFirmaSocio:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        suscripciones: ServicioSuscripcion,
        firma: ServicioFirma,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._suscripciones = suscripciones
        self._firma = firma
        self._reloj = reloj

    # --- acceso y auditoría -----------------------------------------------------------------------------------------

    def _autorizar(self, suscripcion_id: str, socio_client_id: str) -> SuscripcionDB:
        """404 si no existe o no la inició este socio: el criterio 10 pide no revelar que existe."""
        with self._fabrica() as s:
            susc = repo.obtener(s, suscripcion_id)
        if susc is None or susc.socio_client_id != socio_client_id:
            raise SuscripcionNoEncontrada
        return susc

    def _auditar(
        self, socio_client_id: str, accion: str, resultado: str, suscripcion_id: str | None, cotizacion_id: str | None
    ) -> None:
        with self._fabrica() as s, s.begin():
            s.add(
                AuditoriaSocioDB(
                    socio_client_id=socio_client_id,
                    suscripcion_id=suscripcion_id,
                    cotizacion_id=cotizacion_id,
                    accion=accion,
                    resultado=resultado,
                    created_at=self._reloj(),
                )
            )
        log.info(
            "accion del socio socio=%s accion=%s resultado=%s suscripcion_id=%s",
            socio_client_id,
            accion,
            resultado,
            suscripcion_id,
        )

    @asynccontextmanager
    async def _auditando(self, socio_client_id: str, accion: str, suscripcion_id: str) -> AsyncIterator[None]:
        """Registra el resultado de la acción. Solo el código del error: su detalle podría traer datos del cliente."""
        try:
            yield
        except Exception as error:
            # Una suscripción inexistente no se puede referenciar; igual queda el intento.
            referencia = None if isinstance(error, SuscripcionNoEncontrada) else suscripcion_id
            await asyncio.to_thread(
                self._auditar, socio_client_id, accion, getattr(error, "code", ERROR_INTERNO), referencia, None
            )
            raise
        await asyncio.to_thread(self._auditar, socio_client_id, accion, EXITO, suscripcion_id, None)

    # --- iniciar ----------------------------------------------------------------------------------------------------

    async def iniciar(
        self,
        socio_client_id: str,
        cliente_id: str,
        *,
        clave: str | None,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str,
    ) -> tuple[SuscripcionDB, bool]:
        """`acepta_terminos` es la declaración del socio de que el cliente aceptó; la prueba es la firma con OTP (ADR-0026)."""
        try:
            susc, creada = await self._suscripciones.iniciar_socio(
                socio_client_id,
                cliente_id,
                clave=clave,
                cotizacion_id=cotizacion_id,
                metodo_pago=metodo_pago,
                acepta_terminos=acepta_terminos,
                periodicidad_pago=periodicidad_pago,
            )
        except Exception as error:
            await asyncio.to_thread(
                self._auditar,
                socio_client_id,
                INICIAR_SUSCRIPCION,
                getattr(error, "code", ERROR_INTERNO),
                None,
                cotizacion_id[:36],
            )
            raise
        await asyncio.to_thread(self._auditar, socio_client_id, INICIAR_SUSCRIPCION, EXITO, susc.id, cotizacion_id[:36])
        return susc, creada

    # --- consultar --------------------------------------------------------------------------------------------------

    async def detalle(self, suscripcion_id: str, socio_client_id: str) -> SuscripcionDeSocio:
        """Consultar no se audita: el socio la repite hasta ver la póliza y llenaría la tabla sin aportar evidencia."""
        susc = await asyncio.to_thread(self._autorizar, suscripcion_id, socio_client_id)
        poliza = await asyncio.to_thread(self._suscripciones.poliza_de_suscripcion, suscripcion_id)
        return SuscripcionDeSocio(susc, poliza)

    # --- firmar -----------------------------------------------------------------------------------------------------

    async def documento_para_firmar(self, suscripcion_id: str, socio_client_id: str) -> DocumentoParaFirmar:
        susc = await asyncio.to_thread(self._autorizar, suscripcion_id, socio_client_id)
        return await self._firma.documento_para_firmar_como(suscripcion_id, susc.cliente_id)

    async def contenido_documento(self, suscripcion_id: str, socio_client_id: str) -> bytes:
        async with self._auditando(socio_client_id, DESCARGAR_DOCUMENTO, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, socio_client_id)
            return await self._firma.contenido_documento_como(suscripcion_id, susc.cliente_id)

    async def solicitar_codigo(self, suscripcion_id: str, socio_client_id: str) -> None:
        async with self._auditando(socio_client_id, SOLICITAR_CODIGO, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, socio_client_id)
            await self._firma.solicitar_codigo_como(suscripcion_id, susc.cliente_id)

    async def firmar(
        self,
        suscripcion_id: str,
        socio_client_id: str,
        *,
        documento_sha256: str,
        documento_revisado: bool,
        codigo: str,
        origen: OrigenFirma,
    ) -> tuple[FirmaDB, bool]:
        """El firmante es el cliente; el socio solo transporta su firma. Repetirla devuelve la misma firma."""
        async with self._auditando(socio_client_id, FIRMAR, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, socio_client_id)
            return await self._firma.firmar_como(
                suscripcion_id,
                susc.cliente_id,
                documento_sha256=documento_sha256,
                documento_revisado=documento_revisado,
                codigo=codigo,
                origen=replace(origen, modalidad=MODALIDAD_SOCIO, socio_client_id=socio_client_id),
            )

    async def estado_saga(self, suscripcion_id: str) -> str:
        return await self._firma.estado_saga(suscripcion_id)
