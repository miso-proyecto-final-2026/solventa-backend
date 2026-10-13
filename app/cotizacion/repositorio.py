"""Persistencia de cotizaciones, coberturas y registro de latencia (HU12, criterios 7 y 9)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.cotizacion.dominio import CoberturaCalculada, CotizacionEmitida
from app.cotizacion.errores import CotizacionNoEncontrada
from app.cotizacion.modelos_db import (
    SCHEMA,
    BaseCotizacion,
    CoberturaDB,
    CotizacionDB,
    LogLatenciaDB,
)


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
        """Guarda la cotización y sus coberturas en una sola transacción."""
        with self._fabrica() as sesion, sesion.begin():
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
                componentes=(),
                version_reglas=fila.version_reglas,
                degradado=fila.degradado,
                aproximada=fila.aproximada,
                vigente_hasta=_utc(fila.vigente_hasta),
                creada_en=_utc(fila.created_at),
                hit_cache=log.hit_cache if log else False,
                latencia_interna_ms=log.latencia_interna_ms if log else None,
            )

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
