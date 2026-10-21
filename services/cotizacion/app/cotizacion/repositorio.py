"""Persistencia de cotizaciones, coberturas y registro de latencia (HU12, criterios 7 y 9)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.cotizacion.desglose import a_json, desde_json
from app.cotizacion.dominio import (
    CANAL_API,
    CoberturaCalculada,
    CotizacionEmitida,
    DesglosePrecio,
    OrigenCotizacion,
)
from app.cotizacion.errores import CotizacionNoEncontrada
from app.cotizacion.modelos_db import (
    SCHEMA,
    BaseCotizacion,
    CoberturaDB,
    CotizacionDB,
    DesgloseDB,
    LogLatenciaDB,
    TrazabilidadConsultaDB,
)
from app.proveedores.dominio import RegistroTrazabilidad, ResultadoConsulta


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `cotizacion`. En SQLite (dev/pruebas) no hay esquemas,
    así que se traduce el esquema a ninguno."""
    url = normalizar_url(url)
    if url.startswith("sqlite"):
        memoria = ":memory:" in url or url in ("sqlite://", "sqlite:///")
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if memoria else None,
        )
        return engine.execution_options(schema_translate_map={SCHEMA: None})
    return create_engine(url, pool_pre_ping=True)


def crear_tablas_locales(engine: Engine) -> None:
    """Solo para SQLite de desarrollo. En PostgreSQL el esquema lo crea Alembic."""
    BaseCotizacion.metadata.create_all(engine)


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


class RepositorioCotizaciones:
    def __init__(self, fabrica: sessionmaker[Session]) -> None:
        self._fabrica = fabrica

    def guardar(self, c: CotizacionEmitida, datos: dict[str, Any]) -> None:
        """Guarda cotización, coberturas y desglose en una sola transacción (HU14)."""
        with self._fabrica() as sesion, sesion.begin():
            if c.desglose is not None:
                sesion.add(
                    DesgloseDB(
                        cotizacion_id=c.id,
                        version_esquema=c.desglose.version_esquema,
                        detalle=a_json(c.desglose),
                        created_at=c.creada_en,
                    )
                )
            sesion.add(
                CotizacionDB(
                    id=c.id,
                    cliente_id=c.cliente_id,
                    producto=c.producto,
                    moneda=c.moneda,
                    prima_anual=c.prima_anual,
                    prima_mensual=c.prima_mensual,
                    estado=c.estado,
                    version_reglas=c.version_reglas,
                    degradado=c.degradado,
                    aproximada=c.aproximada,
                    datos=datos,
                    vigente_hasta=c.vigente_hasta,
                    created_at=c.creada_en,
                    canal=c.origen.canal if c.origen else CANAL_API,
                    asesor_id=c.origen.asesor_id if c.origen else None,
                    sin_cuenta=c.origen.sin_cuenta if c.origen else False,
                    documento_tipo=c.origen.documento_tipo if c.origen else None,
                    documento_numero=c.origen.documento_numero if c.origen else None,
                    coberturas=[
                        CoberturaDB(
                            orden=i,
                            tipo=cob.tipo,
                            monto=cob.monto,
                            deducible=cob.deducible,
                        )
                        for i, cob in enumerate(c.coberturas)
                    ],
                )
            )

    def guardar_latencia(
        self,
        cotizacion_id: str,
        latencia_interna_ms: float,
        hit_cache: bool,
        degradado: bool,
        momento: datetime,
    ) -> None:
        with self._fabrica() as sesion, sesion.begin():
            sesion.add(
                LogLatenciaDB(
                    cotizacion_id=cotizacion_id,
                    latencia_interna_ms=latencia_interna_ms,
                    hit_cache=hit_cache,
                    degradado=degradado,
                    timestamp=momento,
                )
            )

    def obtener(self, cotizacion_id: str) -> CotizacionEmitida | None:
        with self._fabrica() as sesion:
            fila = sesion.scalar(
                select(CotizacionDB)
                .options(selectinload(CotizacionDB.coberturas))
                .where(CotizacionDB.id == cotizacion_id)
            )
            if fila is None:
                return None
            log = sesion.scalar(
                select(LogLatenciaDB)
                .where(LogLatenciaDB.cotizacion_id == cotizacion_id)
                .order_by(LogLatenciaDB.id)
                .limit(1)
            )
            desglose_fila = sesion.get(DesgloseDB, cotizacion_id)
            desglose = (
                desde_json(desglose_fila.detalle) if desglose_fila is not None else None
            )
            return CotizacionEmitida(
                id=fila.id,
                cliente_id=fila.cliente_id,
                producto=fila.producto,
                estado=fila.estado,
                moneda=fila.moneda,
                prima_anual=Decimal(fila.prima_anual),
                prima_mensual=Decimal(fila.prima_mensual),
                coberturas=tuple(
                    CoberturaCalculada(c.tipo, Decimal(c.monto), Decimal(c.deducible))
                    for c in fila.coberturas
                ),
                version_reglas=fila.version_reglas,
                degradado=fila.degradado,
                aproximada=fila.aproximada,
                vigente_hasta=_utc(fila.vigente_hasta),
                creada_en=_utc(fila.created_at),
                hit_cache=log.hit_cache if log else False,
                latencia_interna_ms=log.latencia_interna_ms if log else None,
                desglose=desglose,
                origen=OrigenCotizacion(
                    canal=fila.canal,
                    asesor_id=fila.asesor_id,
                    sin_cuenta=fila.sin_cuenta,
                    documento_tipo=fila.documento_tipo,
                    documento_numero=fila.documento_numero,
                ),
            )

    def existe(self, cotizacion_id: str) -> bool:
        with self._fabrica() as sesion:
            return sesion.get(CotizacionDB, cotizacion_id) is not None

    def obtener_desglose(self, cotizacion_id: str) -> DesglosePrecio | None:
        """Desglose persistido, o None si la cotización no lo tiene (anterior a HU14)."""
        with self._fabrica() as sesion:
            fila = sesion.get(DesgloseDB, cotizacion_id)
            return None if fila is None else desde_json(fila.detalle)

    def registrar_latencia_cliente(
        self, cotizacion_id: str, latencia_cliente_ms: float
    ) -> None:
        """Completa `latencia_cliente_ms`. Lanza CotizacionNoEncontrada si no hay registro."""
        with self._fabrica() as sesion, sesion.begin():
            resultado = sesion.execute(
                update(LogLatenciaDB)
                .where(LogLatenciaDB.cotizacion_id == cotizacion_id)
                .values(latencia_cliente_ms=latencia_cliente_ms)
            )
            if resultado.rowcount == 0:
                raise CotizacionNoEncontrada(cotizacion_id)


def _fila_trazabilidad(r: RegistroTrazabilidad) -> TrazabilidadConsultaDB:
    return TrazabilidadConsultaDB(
        id=r.id,
        cliente_id=r.cliente_id[:64],
        proveedor=r.proveedor,
        categorias=list(r.categorias),
        consultado_en=r.consultado_en,
        consentimiento_id=r.consentimiento_id[:64] if r.consentimiento_id else None,
        resultado=r.resultado.value,
        latencia_ms=r.latencia_ms,
        hit_cache=r.hit_cache,
        cotizacion_id=r.cotizacion_id,
        correlacion_id=r.correlacion_id,
    )


class RepositorioTrazabilidad:
    """Solo inserta y lee: la huella de las consultas es inmutable (HU11)."""

    def __init__(self, fabrica: sessionmaker[Session]) -> None:
        self._fabrica = fabrica

    def insertar_lote(self, registros: list[RegistroTrazabilidad]) -> None:
        with self._fabrica() as sesion, sesion.begin():
            sesion.add_all([_fila_trazabilidad(r) for r in registros])

    def listar_por_cotizacion(self, cotizacion_id: str) -> list[RegistroTrazabilidad]:
        with self._fabrica() as sesion:
            filas = sesion.scalars(
                select(TrazabilidadConsultaDB)
                .where(TrazabilidadConsultaDB.cotizacion_id == cotizacion_id)
                .order_by(
                    TrazabilidadConsultaDB.consultado_en, TrazabilidadConsultaDB.id
                )
            ).all()
            return [
                RegistroTrazabilidad(
                    id=f.id,
                    cliente_id=f.cliente_id,
                    proveedor=f.proveedor,
                    categorias=tuple(f.categorias),
                    consultado_en=_utc(f.consultado_en),
                    resultado=ResultadoConsulta(f.resultado),
                    latencia_ms=f.latencia_ms,
                    hit_cache=f.hit_cache,
                    consentimiento_id=f.consentimiento_id,
                    cotizacion_id=f.cotizacion_id,
                    correlacion_id=f.correlacion_id,
                )
                for f in filas
            ]
