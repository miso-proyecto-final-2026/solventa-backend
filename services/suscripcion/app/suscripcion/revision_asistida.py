"""Revisión asistida (HU16): la bandeja del asesor, el detalle del caso y su decisión.

Sigue el patrón de `ServicioFirmaAsistida`: cada acción del asesor se audita, también la rechazada. Decidir corre en una sola
transacción con la saga y la revisión bloqueadas, así que de varias réplicas decidiendo el mismo caso solo una gana.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.suscripcion import repositorio as repo
from app.suscripcion.acceso import Asesor
from app.suscripcion.clientes import Cotizaciones, Identidad, ResumenCliente
from app.suscripcion.config import TOPIC_REVISION
from app.suscripcion.dominio import Decision, EstadoSaga, PasoSaga
from app.suscripcion.errores import (
    CotizacionNoDisponible,
    CotizacionVencida,
    DependenciaNoDisponible,
    RevisionNoEncontrada,
    RevisionYaDecidida,
    SinMetodoPago,
)
from app.suscripcion.modelos_db import (
    AuditoriaAsesorDB,
    RevisionAsistidaDB,
    SuscripcionDB,
)
from app.suscripcion.revision import (
    CategoriaMotivo,
    Prioridad,
    UmbralesPrioridad,
    ajustar,
    categorizar,
    prioridad,
)
from app.suscripcion.servicio import encolar, encolar_cobro

log = logging.getLogger("suscripcion.revision")

EXITO = "EXITO"
ERROR_INTERNO = "ERROR_INTERNO"
DECIDIR_REVISION = "DECIDIR_REVISION"
MOTIVO_RECHAZO = "Rechazada tras revisión asistida."
MAX_VIGENCIAS_POR_LISTADO = 50
DECISIONES = ("APROBADA", "RECHAZADA", "AJUSTADA")


def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FilaRevision:
    revision: RevisionAsistidaDB
    suscripcion: SuscripcionDB
    cliente: ResumenCliente | None
    prioridad: Prioridad
    categorias: list[CategoriaMotivo]
    vigente_hasta: datetime | None
    cotizacion_vencida: bool


@dataclass(frozen=True)
class Bandeja:
    filas: list[FilaRevision]
    total: int
    total_vencidas: int


@dataclass(frozen=True)
class DetalleRevision(FilaRevision):
    puede_decidir: bool = False
    decisiones_permitidas: tuple[str, ...] = ()


def motivos_de(susc: SuscripcionDB) -> list[str]:
    """Los motivos de la derivación, sin la frase genérica que se suma al rechazar."""
    return [m for m in (susc.motivos or []) if m != MOTIVO_RECHAZO]


class ServicioRevisionAsistida:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        identidad: Identidad,
        cotizaciones: Cotizaciones,
        umbrales: UmbralesPrioridad = UmbralesPrioridad(),
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._identidad = identidad
        self._cotizaciones = cotizaciones
        self._umbrales = umbrales
        self._reloj = reloj

    # --- auditoría ----------------------------------------------------------------------------------------------------

    def _auditar(self, asesor: Asesor, resultado: str, suscripcion_id: str | None) -> None:
        with self._fabrica() as s, s.begin():
            s.add(
                AuditoriaAsesorDB(
                    asesor_id=asesor.id,
                    suscripcion_id=suscripcion_id,
                    accion=DECIDIR_REVISION,
                    resultado=resultado,
                    created_at=self._reloj(),
                )
            )
        log.info(
            "accion del asesor asesor_id=%s accion=%s resultado=%s suscripcion_id=%s",
            asesor.id, DECIDIR_REVISION, resultado, suscripcion_id,
        )

    # --- vigencia de la cotización --------------------------------------------------------------------------------------

    async def _consultar_vigencia(self, cotizacion_id: str) -> datetime | None:
        """La vigencia según Cotización; nula si no responde (el caso sigue y se reintenta)."""
        try:
            c = await self._cotizaciones.obtener(cotizacion_id)
        except DependenciaNoDisponible:
            log.warning("no se pudo leer la vigencia de la cotizacion cotizacion_id=%s", cotizacion_id)
            return None
        return None if c is None else repo.utc(c.vigente_hasta)

    def _guardar_vigencia(self, suscripcion_id: str, vigente_hasta: datetime) -> None:
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id)
            if susc is not None and susc.cotizacion_vigente_hasta is None:
                susc.cotizacion_vigente_hasta = vigente_hasta

    async def asegurar_vigencias(self) -> None:
        """Llena la vigencia de las revisiones abiertas anteriores a HU16 (como máximo 50 por listado)."""

        def pendientes() -> list[tuple[str, str]]:
            with self._fabrica() as s:
                return [
                    (susc.id, susc.cotizacion_id)
                    for susc in s.execute(
                        select(SuscripcionDB)
                        .join(RevisionAsistidaDB, RevisionAsistidaDB.suscripcion_id == SuscripcionDB.id)
                        .where(RevisionAsistidaDB.decision.is_(None), SuscripcionDB.cotizacion_vigente_hasta.is_(None))
                        .limit(MAX_VIGENCIAS_POR_LISTADO)
                    ).scalars()
                ]

        faltantes = await asyncio.to_thread(pendientes)
        if not faltantes:
            return
        vigencias = await asyncio.gather(*(self._consultar_vigencia(c) for _, c in faltantes))
        for (susc_id, _), vigente in zip(faltantes, vigencias, strict=True):
            if vigente is not None:
                await asyncio.to_thread(self._guardar_vigencia, susc_id, vigente)

    # --- consultar ----------------------------------------------------------------------------------------------------

    async def _resumen(self, cliente_id: str) -> ResumenCliente | None:
        """Sin Identidad la fila se muestra sin cliente: la revisión no depende de eso."""
        try:
            return await self._identidad.resumen_cliente(cliente_id)
        except DependenciaNoDisponible:
            log.warning("no se pudo leer el resumen del cliente")
            return None

    @staticmethod
    def _vencida(susc: SuscripcionDB, revision: RevisionAsistidaDB, ahora: datetime) -> bool:
        vigente = repo.utc(susc.cotizacion_vigente_hasta)
        return revision.decision is None and vigente is not None and vigente <= ahora

    def _fila(
        self, revision: RevisionAsistidaDB, susc: SuscripcionDB, cliente: ResumenCliente | None, ahora: datetime
    ) -> FilaRevision:
        creada = repo.utc(revision.created_at)
        assert creada is not None
        return FilaRevision(
            revision=revision,
            suscripcion=susc,
            cliente=cliente,
            prioridad=prioridad(creada, ahora, self._umbrales),
            categorias=list(dict.fromkeys(categorizar(m) for m in motivos_de(susc))),
            vigente_hasta=repo.utc(susc.cotizacion_vigente_hasta),
            cotizacion_vencida=self._vencida(susc, revision, ahora),
        )

    async def listar(
        self,
        asesor: Asesor,
        *,
        estado: str = "PENDIENTE",
        categoria: CategoriaMotivo | None = None,
        solo_mias: bool = False,
        vencidas: bool = False,
        pagina: int = 1,
        tamano: int = 20,
    ) -> Bandeja:
        await self.asegurar_vigencias()
        ahora = self._reloj()

        def leer() -> tuple[list[tuple[RevisionAsistidaDB, SuscripcionDB]], int, int]:
            pendiente = RevisionAsistidaDB.decision.is_(None)
            condiciones = [pendiente if estado == "PENDIENTE" else RevisionAsistidaDB.decision.is_not(None)]
            if solo_mias:
                condiciones.append(SuscripcionDB.asesor_id == asesor.id)
            if vencidas:
                condiciones += [pendiente, SuscripcionDB.cotizacion_vigente_hasta <= ahora]
            union = RevisionAsistidaDB.suscripcion_id == SuscripcionDB.id
            orden = (RevisionAsistidaDB.created_at, RevisionAsistidaDB.id)
            with self._fabrica() as s:
                total_vencidas = s.execute(
                    select(func.count())
                    .select_from(RevisionAsistidaDB)
                    .join(SuscripcionDB, union)
                    .where(pendiente, SuscripcionDB.cotizacion_vigente_hasta <= ahora)
                ).scalar_one()
                consulta = select(RevisionAsistidaDB, SuscripcionDB).join(SuscripcionDB, union).where(*condiciones).order_by(*orden)
                if categoria is None:
                    total = s.execute(
                        select(func.count()).select_from(RevisionAsistidaDB).join(SuscripcionDB, union).where(*condiciones)
                    ).scalar_one()
                    filas = s.execute(consulta.limit(tamano).offset((pagina - 1) * tamano)).all()
                    return [(r, x) for r, x in filas], total, total_vencidas
                # La categoría sale del texto de los motivos: se filtra en memoria (la bandeja es pequeña).
                todas = [(r, x) for r, x in s.execute(consulta).all()]
                coinciden = [(r, x) for r, x in todas if categoria in {categorizar(m) for m in motivos_de(x)}]
                inicio = (pagina - 1) * tamano
                return coinciden[inicio : inicio + tamano], len(coinciden), total_vencidas

        pares, total, total_vencidas = await asyncio.to_thread(leer)
        ids = list({x.cliente_id for _, x in pares})
        resumenes = dict(zip(ids, await asyncio.gather(*(self._resumen(i) for i in ids)), strict=True))
        filas = [self._fila(r, x, resumenes[x.cliente_id], ahora) for r, x in pares]
        return Bandeja(filas=filas, total=total, total_vencidas=total_vencidas)

    def _revision_de(self, suscripcion_id: str) -> tuple[RevisionAsistidaDB, SuscripcionDB]:
        with self._fabrica() as s:
            susc = repo.obtener(s, suscripcion_id)
            revisiones = repo.revisiones_de(s, suscripcion_id)
        if susc is None or not revisiones:
            raise RevisionNoEncontrada
        return revisiones[-1], susc

    async def detalle(self, suscripcion_id: str) -> DetalleRevision:
        """Cualquier asesor ve el caso: la bandeja es compartida."""
        revision, susc = await asyncio.to_thread(self._revision_de, suscripcion_id)
        if revision.decision is None and susc.cotizacion_vigente_hasta is None:
            vigente = await self._consultar_vigencia(susc.cotizacion_id)
            if vigente is not None:
                await asyncio.to_thread(self._guardar_vigencia, susc.id, vigente)
                susc.cotizacion_vigente_hasta = vigente
        ahora = self._reloj()
        fila = self._fila(revision, susc, await self._resumen(susc.cliente_id), ahora)
        abierta = revision.decision is None and (susc.estado_saga, susc.paso_actual, susc.decision) == (
            EstadoSaga.EN_PROGRESO.value, PasoSaga.DECISION.value, Decision.DERIVADA.value
        )
        permitidas = () if not abierta else (("RECHAZADA",) if fila.cotizacion_vencida else DECISIONES)
        return DetalleRevision(**fila.__dict__, puede_decidir=abierta, decisiones_permitidas=permitidas)

    # --- decidir ------------------------------------------------------------------------------------------------------

    async def decidir(
        self, asesor: Asesor, suscripcion_id: str, decision: str, justificacion: str, monto_asegurado: int | None = None
    ) -> DetalleRevision:
        try:
            await self._asegurar_vigencia_para(suscripcion_id, decision)
            await asyncio.to_thread(self._decidir, asesor, suscripcion_id, decision, justificacion, monto_asegurado)
        except Exception as error:
            referencia = None if isinstance(error, RevisionNoEncontrada) else suscripcion_id
            await asyncio.to_thread(self._auditar, asesor, getattr(error, "code", ERROR_INTERNO), referencia)
            raise
        await asyncio.to_thread(self._auditar, asesor, EXITO, suscripcion_id)
        return await self.detalle(suscripcion_id)

    async def _asegurar_vigencia_para(self, suscripcion_id: str, decision: str) -> None:
        """Aprobar o ajustar cobran, así que la vigencia tiene que conocerse. Rechazar no la necesita."""
        if decision == "RECHAZADA":
            return

        def leer() -> tuple[str, datetime | None] | None:
            with self._fabrica() as s:
                susc = repo.obtener(s, suscripcion_id)
                return None if susc is None else (susc.cotizacion_id, susc.cotizacion_vigente_hasta)

        encontrada = await asyncio.to_thread(leer)
        if encontrada is None or encontrada[1] is not None:
            return  # sin saga lo dirá `_decidir` (404); con vigencia no hay nada que consultar
        vigente = await self._consultar_vigencia(encontrada[0])
        if vigente is None:
            raise CotizacionNoDisponible
        await asyncio.to_thread(self._guardar_vigencia, suscripcion_id, vigente)

    def _decidir(self, asesor: Asesor, suscripcion_id: str, decision: str, justificacion: str, monto: int | None) -> None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            susc = s.get(SuscripcionDB, suscripcion_id, with_for_update=True)
            if susc is None:
                raise RevisionNoEncontrada
            revision = s.execute(
                select(RevisionAsistidaDB)
                .where(RevisionAsistidaDB.suscripcion_id == suscripcion_id, RevisionAsistidaDB.decision.is_(None))
                .with_for_update()
            ).scalar_one_or_none()
            if revision is None:
                previas = repo.revisiones_de(s, suscripcion_id)
                if not previas:
                    raise RevisionNoEncontrada
                raise self._ya_decidida(previas[-1])
            if (susc.estado_saga, susc.paso_actual, susc.decision) != (
                EstadoSaga.EN_PROGRESO.value, PasoSaga.DECISION.value, Decision.DERIVADA.value
            ):
                raise RevisionYaDecidida
            cobra = decision != "RECHAZADA"
            vigente = repo.utc(susc.cotizacion_vigente_hasta)
            if cobra and vigente is not None and vigente <= ahora:
                raise CotizacionVencida(detalles=[{"campo": "vigente_hasta", "motivo": vigente.isoformat()}])
            if cobra and not susc.metodo_pago_token:
                raise SinMetodoPago
            ajuste = None
            if decision == "AJUSTADA":
                ajuste = ajustar(
                    monto_asegurado=int(susc.monto_asegurado),
                    prima_mensual=int(susc.prima_mensual),
                    prima_anual=int(susc.prima_anual),
                    coberturas=susc.coberturas,
                    nuevo_monto=int(monto or 0),
                )
                susc.monto_asegurado = ajuste.monto_asegurado
                susc.prima_mensual = Decimal(ajuste.prima_mensual)
                susc.prima_anual = Decimal(ajuste.prima_anual)
                if susc.coberturas:
                    susc.coberturas = ajuste.coberturas
                susc.ajuste = ajuste.como_dict()

            revision.decision = decision
            revision.justificacion = justificacion
            revision.ajustes = None if ajuste is None else ajuste.como_dict()
            revision.asesor_id = asesor.id
            revision.asesor_nombre = asesor.nombre[:120] if asesor.nombre else None
            revision.decidido_en = ahora

            # La decisión humana pasa a ser la de la saga, para que el cliente la vea como una automática (la derivación
            # original queda en la revisión y en el outbox). `decidido_en` y la duración siguen siendo los de HU15.
            susc.decision = decision
            susc.updated_at = ahora
            if cobra:
                susc.estado_saga, susc.paso_actual = EstadoSaga.APROBADA.value, PasoSaga.COBRO.value
                encolar_cobro(s, susc, ahora)
            else:
                susc.estado_saga, susc.paso_actual = EstadoSaga.RECHAZADA.value, PasoSaga.DECISION.value
                susc.motivos = [*(susc.motivos or []), MOTIVO_RECHAZO]
                susc.metodo_pago_token = None
            extra: dict[str, Any] = {"asesor_id": asesor.id, "decision_origen": Decision.DERIVADA.value}
            if ajuste is not None:
                extra["ajuste"] = ajuste.como_dict()
            encolar(s, susc, "revision_asistida_resuelta", TOPIC_REVISION, ahora, extra)  # sin la justificación: texto libre
        log.info("revision decidida suscripcion_id=%s decision=%s asesor_id=%s", suscripcion_id, decision, asesor.id)

    @staticmethod
    def _ya_decidida(revision: RevisionAsistidaDB) -> RevisionYaDecidida:
        decidido = repo.utc(revision.decidido_en)
        return RevisionYaDecidida(
            detalles=[
                {"campo": "decision", "motivo": revision.decision or ""},
                {"campo": "asesor", "motivo": revision.asesor_nombre or revision.asesor_id or ""},
                {"campo": "decidido_en", "motivo": decidido.isoformat() if decidido else ""},
            ]
        )
