"""Orquestación de la verificación KYC: validaciones, evidencias, proveedor y estado del cliente."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.kyc.config import ConfigKyc
from app.kyc.dominio import (
    MOTIVO_NO_SUPERADA,
    MOTIVO_PROVEEDOR_NO_DISPONIBLE,
    RETO_VIDA,
    AlmacenEvidencias,
    DecisionProveedor,
    EnRevision,
    EstadoKyc,
    ImagenesKyc,
    IntentosAgotados,
    ProveedorKycError,
    RetoInvalido,
    SolicitudVerificacion,
    VerificadorIdentidad,
    YaVerificado,
)
from app.models import Cliente, VerificacionKyc

log = logging.getLogger("identidad.kyc")

_EXTENSIONES = {"image/jpeg": "jpg", "image/png": "png"}


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


class ServicioVerificacion:
    def __init__(
        self,
        verificador: VerificadorIdentidad,
        almacen: AlmacenEvidencias,
        config: ConfigKyc,
        reloj: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._verificador = verificador
        self._almacen = almacen
        self.config = config
        self._reloj = reloj

    # -- consultas -------------------------------------------------------------------------

    def _rechazos_en_ventana(self, db: Session, cliente_id: str) -> list[VerificacionKyc]:
        desde = self._reloj() - timedelta(minutes=self.config.bloqueo_min)
        return (
            db.query(VerificacionKyc)
            .filter(
                VerificacionKyc.cliente_id == cliente_id,
                VerificacionKyc.estado == EstadoKyc.RECHAZADO.value,
                VerificacionKyc.creado_en >= desde,
            )
            .order_by(VerificacionKyc.creado_en)
            .all()
        )

    def intentos_restantes(self, db: Session, cliente_id: str) -> int:
        usados = len(self._rechazos_en_ventana(db, cliente_id))
        return max(0, self.config.max_intentos - usados)

    def bloqueado_hasta(self, db: Session, cliente_id: str) -> datetime | None:
        rechazos = self._rechazos_en_ventana(db, cliente_id)
        if len(rechazos) < self.config.max_intentos:
            return None
        # El bloqueo termina cuando el rechazo más antiguo de la ventana sale de ella.
        return _utc(rechazos[-self.config.max_intentos].creado_en) + timedelta(
            minutes=self.config.bloqueo_min
        )

    def ultima(self, db: Session, cliente_id: str) -> VerificacionKyc | None:
        return (
            db.query(VerificacionKyc)
            .filter(VerificacionKyc.cliente_id == cliente_id)
            .order_by(VerificacionKyc.creado_en.desc())
            .first()
        )

    def _por_clave(self, db: Session, cliente_id: str, clave: str) -> VerificacionKyc | None:
        return (
            db.query(VerificacionKyc)
            .filter(
                VerificacionKyc.cliente_id == cliente_id,
                VerificacionKyc.clave_idempotencia == clave,
            )
            .first()
        )

    # -- verificación ----------------------------------------------------------------------

    async def verificar(
        self,
        db: Session,
        cliente: Cliente,
        clave: str,
        reto: str,
        imagenes: ImagenesKyc,
        tipos: dict[str, str],
    ) -> tuple[VerificacionKyc, bool]:
        """Devuelve (verificación, creada). `creada=False` es una repetición idempotente."""
        previa = self._por_clave(db, cliente.id, clave)
        if previa is not None:
            return previa, False

        if reto != RETO_VIDA:
            raise RetoInvalido("El reto de prueba de vida no es válido")
        if cliente.estado_kyc == EstadoKyc.VERIFICADO.value:
            raise YaVerificado("La identidad ya fue verificada")
        if cliente.estado_kyc == EstadoKyc.EN_REVISION.value:
            raise EnRevision("La verificación está en revisión")
        hasta = self.bloqueado_hasta(db, cliente.id)
        if hasta is not None and hasta > self._reloj():
            raise IntentosAgotados(hasta)

        verificacion_id = str(uuid.uuid4())
        # Las imágenes se guardan antes de llamar al proveedor: si este falla no se pierden.
        evidencias = await self._guardar_evidencias(cliente.id, verificacion_id, imagenes, tipos)

        estado, motivo, referencia = await self._consultar_proveedor(
            SolicitudVerificacion(cliente.id, clave, reto, imagenes)
        )

        verificacion = VerificacionKyc(
            id=verificacion_id,
            cliente_id=cliente.id,
            clave_idempotencia=clave,
            estado=estado.value,
            motivo=motivo,
            referencia_proveedor=referencia,
            reto=reto,
            evidencias=evidencias,
            creado_en=self._reloj(),
        )
        db.add(verificacion)
        cliente.estado_kyc = estado.value
        try:
            db.commit()
        except IntegrityError:
            # Un reintento concurrente con la misma clave ganó la inserción.
            db.rollback()
            existente = self._por_clave(db, cliente.id, clave)
            if existente is None:
                raise
            return existente, False
        log.info(
            "kyc_verificacion cliente=%s verificacion=%s estado=%s", cliente.id, verificacion_id, estado.value
        )
        return verificacion, True

    async def _guardar_evidencias(
        self, cliente_id: str, verificacion_id: str, imagenes: ImagenesKyc, tipos: dict[str, str]
    ) -> dict[str, str]:
        partes = {"anverso": imagenes.anverso, "reverso": imagenes.reverso, "selfie": imagenes.selfie}
        claves: dict[str, str] = {}
        for parte, contenido in partes.items():
            content_type = tipos[parte]
            clave = f"kyc/{cliente_id}/{verificacion_id}/{parte}.{_EXTENSIONES[content_type]}"
            await asyncio.to_thread(self._almacen.guardar, clave, contenido, content_type)
            claves[parte] = clave
        return claves

    async def _consultar_proveedor(
        self, solicitud: SolicitudVerificacion
    ) -> tuple[EstadoKyc, str | None, str | None]:
        try:
            resultado = await asyncio.wait_for(
                self._verificador.verificar(solicitud), timeout=self.config.timeout_s
            )
        except (TimeoutError, ProveedorKycError) as exc:
            # Degradación controlada: el cliente no es rechazado, el caso queda en revisión.
            log.warning(
                "kyc_proveedor_degradado cliente=%s causa=%s", solicitud.cliente_id, type(exc).__name__
            )
            return EstadoKyc.EN_REVISION, MOTIVO_PROVEEDOR_NO_DISPONIBLE, None
        if resultado.decision is DecisionProveedor.APROBADA:
            return EstadoKyc.VERIFICADO, None, resultado.referencia
        return EstadoKyc.RECHAZADO, MOTIVO_NO_SUPERADA, resultado.referencia
