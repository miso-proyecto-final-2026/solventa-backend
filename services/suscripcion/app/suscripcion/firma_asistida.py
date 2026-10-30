"""Venta asistida (HU39): lo que el asesor hace sobre la saga de sus cotizaciones.

No repite reglas: inicia con `ServicioSuscripcion` y firma con `ServicioFirma`, los mismos caminos del cliente. Lo que agrega es
quién puede (solo el asesor de la cotización) y el registro de cada acción, también de las rechazadas.
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
from app.suscripcion.acceso import Asesor
from app.suscripcion.clientes import Identidad, ResumenCliente
from app.suscripcion.errores import (
    AccesoDenegado,
    DependenciaNoDisponible,
    SuscripcionNoEncontrada,
)
from app.suscripcion.firma import (
    MODALIDAD_ASISTIDA_PANTALLA,
    DocumentoParaFirmar,
    OrigenFirma,
    ServicioFirma,
    firma_gestionable,
)
from app.suscripcion.modelos_db import (
    AuditoriaAsesorDB,
    FirmaDB,
    PolizaDB,
    SuscripcionDB,
)
from app.suscripcion.servicio import ServicioSuscripcion

log = logging.getLogger("suscripcion.asistida")

EXITO = "EXITO"
ERROR_INTERNO = "ERROR_INTERNO"
INICIAR_SUSCRIPCION = "INICIAR_SUSCRIPCION"
SOLICITAR_CODIGO = "SOLICITAR_CODIGO"
FIRMAR = "FIRMAR"
DESCARGAR_DOCUMENTO = "DESCARGAR_DOCUMENTO"
DESCARGAR_FIRMADA = "DESCARGAR_FIRMADA"


def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SuscripcionAsistida:
    suscripcion: SuscripcionDB
    poliza: PolizaDB | None
    cliente: ResumenCliente | None
    gestionable: bool


class ServicioFirmaAsistida:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        suscripciones: ServicioSuscripcion,
        firma: ServicioFirma,
        identidad: Identidad,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._suscripciones = suscripciones
        self._firma = firma
        self._identidad = identidad
        self._reloj = reloj

    # --- acceso y auditoría -----------------------------------------------------------------------------------------

    def _autorizar(self, suscripcion_id: str, asesor: Asesor) -> SuscripcionDB:
        """404 si no existe; 403 si no viene de una cotización de este asesor (criterio 6)."""
        with self._fabrica() as s:
            susc = repo.obtener(s, suscripcion_id)
        if susc is None:
            raise SuscripcionNoEncontrada
        if susc.asesor_id != asesor.id:
            raise AccesoDenegado
        return susc

    def _auditar(self, asesor: Asesor, accion: str, resultado: str, suscripcion_id: str | None, cotizacion_id: str | None) -> None:
        with self._fabrica() as s, s.begin():
            s.add(
                AuditoriaAsesorDB(
                    asesor_id=asesor.id,
                    suscripcion_id=suscripcion_id,
                    cotizacion_id=cotizacion_id,
                    accion=accion,
                    resultado=resultado,
                    created_at=self._reloj(),
                )
            )
        log.info("accion del asesor asesor_id=%s accion=%s resultado=%s suscripcion_id=%s", asesor.id, accion, resultado, suscripcion_id)

    @asynccontextmanager
    async def _auditando(self, asesor: Asesor, accion: str, suscripcion_id: str) -> AsyncIterator[None]:
        """Registra el resultado de la acción. Solo el código del error: su detalle podría traer datos del cliente."""
        try:
            yield
        except Exception as error:
            # Una suscripción inexistente no se puede referenciar; igual queda el intento.
            referencia = None if isinstance(error, SuscripcionNoEncontrada) else suscripcion_id
            await asyncio.to_thread(self._auditar, asesor, accion, getattr(error, "code", ERROR_INTERNO), referencia, None)
            raise
        await asyncio.to_thread(self._auditar, asesor, accion, EXITO, suscripcion_id, None)

    # --- iniciar ----------------------------------------------------------------------------------------------------

    async def iniciar(
        self,
        asesor: Asesor,
        *,
        clave: str | None,
        cotizacion_id: str,
        metodo_pago: str,
        acepta_terminos: bool,
        periodicidad_pago: str,
    ) -> tuple[SuscripcionDB, bool]:
        """`acepta_terminos` es la declaración del asesor de que el cliente está al tanto y aceptó (ADR-0020)."""
        try:
            susc, creada = await self._suscripciones.iniciar_asistida(
                asesor.id,
                clave=clave,
                cotizacion_id=cotizacion_id,
                metodo_pago=metodo_pago,
                acepta_terminos=acepta_terminos,
                periodicidad_pago=periodicidad_pago,
            )
        except Exception as error:
            await asyncio.to_thread(
                self._auditar, asesor, INICIAR_SUSCRIPCION, getattr(error, "code", ERROR_INTERNO), None, cotizacion_id[:36]
            )
            raise
        await asyncio.to_thread(self._auditar, asesor, INICIAR_SUSCRIPCION, EXITO, susc.id, cotizacion_id)
        return susc, creada

    # --- consultar --------------------------------------------------------------------------------------------------

    async def _resumen(self, cliente_id: str) -> ResumenCliente | None:
        """Sin Identidad la fila se muestra sin cliente: la saga no depende de eso."""
        try:
            return await self._identidad.resumen_cliente(cliente_id)
        except DependenciaNoDisponible:
            log.warning("no se pudo leer el resumen del cliente")
            return None

    def _armar(self, susc: SuscripcionDB, poliza: PolizaDB | None, cliente: ResumenCliente | None) -> SuscripcionAsistida:
        return SuscripcionAsistida(
            suscripcion=susc,
            poliza=poliza,
            cliente=cliente,
            gestionable=firma_gestionable(susc, poliza, self._firma.vence_en(susc), self._reloj()),
        )

    async def listar(
        self, asesor: Asesor, *, estado: str | None, pagina: int, tamano: int
    ) -> tuple[list[SuscripcionAsistida], int]:
        def leer() -> tuple[list[tuple[SuscripcionDB, PolizaDB | None]], int]:
            with self._fabrica() as s:
                return repo.suscripciones_de_asesor(s, asesor.id, estado=estado, pagina=pagina, tamano=tamano)

        filas, total = await asyncio.to_thread(leer)
        # Una consulta por cliente distinto de la página, acotada por `tamano`.
        ids = list({s.cliente_id for s, _ in filas})
        resumenes = dict(zip(ids, await asyncio.gather(*(self._resumen(i) for i in ids)), strict=True))
        return [self._armar(s, p, resumenes[s.cliente_id]) for s, p in filas], total

    async def detalle(self, suscripcion_id: str, asesor: Asesor) -> SuscripcionAsistida:
        susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
        poliza = await asyncio.to_thread(self._suscripciones.poliza_de_suscripcion, suscripcion_id)
        return self._armar(susc, poliza, await self._resumen(susc.cliente_id))

    # --- firmar -----------------------------------------------------------------------------------------------------

    async def documento_para_firmar(self, suscripcion_id: str, asesor: Asesor) -> DocumentoParaFirmar:
        susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
        return await self._firma.documento_para_firmar_como(suscripcion_id, susc.cliente_id)

    async def contenido_documento(self, suscripcion_id: str, asesor: Asesor) -> bytes:
        async with self._auditando(asesor, DESCARGAR_DOCUMENTO, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
            return await self._firma.contenido_documento_como(suscripcion_id, susc.cliente_id)

    async def solicitar_codigo(self, suscripcion_id: str, asesor: Asesor) -> None:
        async with self._auditando(asesor, SOLICITAR_CODIGO, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
            await self._firma.solicitar_codigo_como(suscripcion_id, susc.cliente_id)

    async def firmar(
        self,
        suscripcion_id: str,
        asesor: Asesor,
        *,
        documento_sha256: str,
        documento_revisado: bool,
        codigo: str,
        origen: OrigenFirma,
    ) -> tuple[FirmaDB, bool]:
        """Si el cliente ya firmó por su canal, devuelve esa misma firma: la evidencia no se sobrescribe."""
        async with self._auditando(asesor, FIRMAR, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
            return await self._firma.firmar_como(
                suscripcion_id,
                susc.cliente_id,
                documento_sha256=documento_sha256,
                documento_revisado=documento_revisado,
                codigo=codigo,
                origen=replace(origen, modalidad=MODALIDAD_ASISTIDA_PANTALLA, asesor_id=asesor.id),
            )

    async def estado_saga(self, suscripcion_id: str) -> str:
        return await self._firma.estado_saga(suscripcion_id)

    async def copia_firmada(self, suscripcion_id: str, asesor: Asesor) -> tuple[bytes, str]:
        async with self._auditando(asesor, DESCARGAR_FIRMADA, suscripcion_id):
            susc = await asyncio.to_thread(self._autorizar, suscripcion_id, asesor)
            return await self._firma.copia_firmada_como(suscripcion_id, susc.cliente_id)
