"""Firma por enlace (HU39): el asesor envía un enlace por correo y el cliente firma sin sesión, con el enlace y su código.

No repite reglas: firma con `ServicioFirma`, igual que móvil y la consola. Lo que agrega es el enlace:

- **El token no se guarda ni viaja por Kafka.** Es un HMAC del id del enlace y su vencimiento; el evento solo lleva el id, y
  Notificaciones pide el token al entregar el correo. La fila guarda el estado, que el token no puede expresar.
- **Un solo enlace activo por suscripción:** enviar otro reemplaza al anterior (criterio 7), y un índice único parcial lo
  garantiza aunque dos réplicas envíen a la vez.
- **Un solo uso:** lo garantiza la restricción de una firma por suscripción, incluso si el proceso cae antes de marcar el
  enlace. Uno ya usado solo permite ver que la póliza quedó firmada y repetir la firma con el mismo resultado.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contexto import traza_id
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.certificado import FirmadorEnlaces
from app.suscripcion.config import TOPIC_CICLO_VIDA
from app.suscripcion.dominio import EstadoSaga
from app.suscripcion.errores import (
    CorreoMuySeguido,
    EnlaceInvalido,
    EnlaceReemplazado,
    EnlacesNoConfigurados,
    EnlaceVencido,
    FirmaNoDisponible,
)
from app.suscripcion.firma import (
    MODALIDAD_ASISTIDA_ENLACE,
    DocumentoParaFirmar,
    OrigenFirma,
    ServicioFirma,
    firma_gestionable,
)
from app.suscripcion.modelos_db import EnlaceFirmaDB, FirmaDB, PolizaDB, SuscripcionDB
from app.suscripcion.servicio import FUENTE

log = logging.getLogger("suscripcion.enlace_firma")

ACTIVO = "ACTIVO"
REEMPLAZADO = "REEMPLAZADO"
USADO = "USADO"
EVENTO_ENLACE_FIRMA = "firma_enlace_solicitada"
ENVIAR_ENLACE_FIRMA = "ENVIAR_ENLACE_FIRMA"
IDIOMA_DEL_TOKEN = "es-CO"  # el firmador lo exige; el idioma de la página va en la ruta


def _ahora() -> datetime:
    return datetime.now(UTC)


class ServicioEnlaceFirma:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        firma: ServicioFirma,
        firmador: FirmadorEnlaces | None,
        vigencia: timedelta = timedelta(hours=24),
        espera_correo: timedelta = timedelta(seconds=60),
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._firma = firma
        self._firmador = firmador
        self._vigencia = vigencia
        self._espera_correo = espera_correo
        self._reloj = reloj

    # --- enviar (lo pide el asesor; `ServicioFirmaAsistida` autoriza y audita) --------------------------------------------

    def crear(self, suscripcion_id: str, asesor_id: str) -> EnlaceFirmaDB:
        """Reemplaza el enlace activo y encola el correo, en la misma transacción."""
        if self._firmador is None:
            raise EnlacesNoConfigurados
        ahora = self._reloj()
        try:
            with self._fabrica() as s, s.begin():
                susc = repo.obtener(s, suscripcion_id)
                poliza = repo.poliza_de_suscripcion(s, suscripcion_id)
                if not firma_gestionable(susc, poliza, self._firma.vence_en(susc), ahora):
                    raise FirmaNoDisponible
                if repo.accion_reciente(s, suscripcion_id, ENVIAR_ENLACE_FIRMA, ahora, self._espera_correo):
                    raise CorreoMuySeguido
                s.execute(
                    update(EnlaceFirmaDB)
                    .where(EnlaceFirmaDB.suscripcion_id == suscripcion_id, EnlaceFirmaDB.estado == ACTIVO)
                    .values(estado=REEMPLAZADO)
                )
                limite = self._firma.vence_en(susc)
                vence = ahora + self._vigencia
                enlace = EnlaceFirmaDB(
                    id=str(uuid4()),
                    suscripcion_id=suscripcion_id,
                    asesor_id=asesor_id,
                    estado=ACTIVO,
                    vence_en=min(vence, limite) if limite else vence,
                    creado_en=ahora,
                )
                s.add(enlace)
                s.flush()
                self._encolar(s, enlace, susc, poliza, ahora)
        except IntegrityError:
            raise CorreoMuySeguido from None  # otra réplica creó el enlace en este mismo instante
        log.info("enlace de firma creado enlace_id=%s suscripcion_id=%s", enlace.id, suscripcion_id)
        return enlace

    def _encolar(self, s: Session, enlace: EnlaceFirmaDB, susc: SuscripcionDB, poliza: PolizaDB, ahora: datetime) -> None:
        campos: dict[str, Any] = {"trace_id": traza_id.get()} if traza_id.get() else {}
        evento = Evento(
            event_type=EVENTO_ENLACE_FIRMA,
            source=FUENTE,
            timestamp=ahora,
            correlation_id=susc.correlacion_id or susc.id,
            payload={
                "enlace_id": enlace.id,
                "suscripcion_id": susc.id,
                "cliente_id": susc.cliente_id,
                "numero_poliza": poliza.numero_poliza,
                "producto": susc.producto,
                "vence_en": repo.utc(enlace.vence_en).isoformat(),
            },
            **campos,
        )
        repo.encolar_evento(
            s, event_id=evento.event_id, topic=TOPIC_CICLO_VIDA, clave=poliza.id, payload=evento.model_dump(mode="json"), ahora=ahora
        )

    # --- token (lo pide Notificaciones al entregar) ---------------------------------------------------------------------

    async def token(self, enlace_id: str) -> tuple[str, datetime]:
        """El token de un enlace activo. Si ya no lo está, 410: un reintento del correo nunca envía un enlace invalidado."""
        if self._firmador is None:
            raise EnlacesNoConfigurados
        ahora = self._reloj()

        def leer() -> EnlaceFirmaDB | None:
            with self._fabrica() as s:
                return s.get(EnlaceFirmaDB, enlace_id)

        enlace = await asyncio.to_thread(leer)
        if enlace is None or enlace.estado != ACTIVO or ahora >= repo.utc(enlace.vence_en):
            raise EnlaceReemplazado
        vence = repo.utc(enlace.vence_en)
        return self._firmador.firmar(enlace.id, IDIOMA_DEL_TOKEN, vence), vence

    # --- lo que hace el cliente con el enlace ---------------------------------------------------------------------------

    def _vigente(self, token: str | None) -> tuple[EnlaceFirmaDB, SuscripcionDB]:
        """401 si el token no es válido; 410 si venció o fue reemplazado."""
        if self._firmador is None:
            raise EnlacesNoConfigurados
        if not token:
            raise EnlaceInvalido
        enlace_id, _ = self._firmador.verificar(token, self._reloj())
        with self._fabrica() as s:
            enlace = s.get(EnlaceFirmaDB, enlace_id)
            susc = repo.obtener(s, enlace.suscripcion_id) if enlace is not None else None
        if enlace is None or susc is None:
            raise EnlaceInvalido
        if enlace.estado == REEMPLAZADO:
            raise EnlaceReemplazado
        if self._reloj() >= repo.utc(enlace.vence_en):
            raise EnlaceVencido("El enlace venció. Pídale uno nuevo a su asesor.")
        return enlace, susc

    async def documento(self, token: str | None) -> DocumentoParaFirmar:
        _, susc = await asyncio.to_thread(self._vigente, token)
        return await self._firma.documento_para_firmar_como(susc.id, susc.cliente_id)

    async def pdf(self, token: str | None) -> bytes:
        _, susc = await asyncio.to_thread(self._vigente, token)
        if susc.estado_saga in (EstadoSaga.FIRMADA.value, EstadoSaga.EMITIDA.value):
            raise FirmaNoDisponible  # ya firmada, el enlace no entrega más el documento
        return await self._firma.contenido_documento_como(susc.id, susc.cliente_id)

    async def solicitar_codigo(self, token: str | None) -> None:
        _, susc = await asyncio.to_thread(self._vigente, token)
        await self._firma.solicitar_codigo_como(susc.id, susc.cliente_id)

    async def firmar(
        self, token: str | None, *, documento_sha256: str, documento_revisado: bool, codigo: str, origen: OrigenFirma
    ) -> tuple[FirmaDB, bool, str]:
        """(firma, creada, suscripción). El firmante es el cliente; `asesor_id` es quien envió el enlace."""
        enlace, susc = await asyncio.to_thread(self._vigente, token)
        firma, creada = await self._firma.firmar_como(
            susc.id,
            susc.cliente_id,
            documento_sha256=documento_sha256,
            documento_revisado=documento_revisado,
            codigo=codigo,
            origen=replace(origen, modalidad=MODALIDAD_ASISTIDA_ENLACE, asesor_id=enlace.asesor_id),
        )
        await asyncio.to_thread(self._marcar_usado, enlace.id)
        return firma, creada, susc.id

    def _marcar_usado(self, enlace_id: str) -> None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            s.execute(
                update(EnlaceFirmaDB)
                .where(EnlaceFirmaDB.id == enlace_id, EnlaceFirmaDB.estado == ACTIVO)
                .values(estado=USADO, usado_en=ahora)
            )

    async def estado_saga(self, suscripcion_id: str) -> str:
        return await self._firma.estado_saga(suscripcion_id)

