"""Persistencia de Perfilamiento. Solo inserción y consultas: nada se actualiza ni se borra."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.perfilamiento.modelos_db import (
    SCHEMA,
    BasePerfilamiento,
    IntentoBloqueadoDB,
    LinajeFuenteDB,
    PerfilEnriquecidoDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `perfilamiento`. En SQLite (dev/pruebas) no hay esquemas."""
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
    BasePerfilamiento.metadata.create_all(engine)


def _utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


@dataclass(frozen=True)
class LineaLinaje:
    fuente: str
    campos: tuple[str, ...]
    obtenido_en: datetime | None
    ttl_s: int | None
    hit_cache: bool
    degradado: bool
    por_defecto: bool


@dataclass(frozen=True)
class PerfilGuardado:
    id: str
    cliente_id: str
    perfil: dict[str, Any]
    completitud: float
    degradado: bool
    consentimiento_id: str | None
    creado_en: datetime


@dataclass(frozen=True)
class LinajeGuardado:
    perfil_id: str
    cliente_id: str
    creado_en: datetime
    consentimiento_id: str | None
    fuentes: tuple[LineaLinaje, ...]


@dataclass(frozen=True)
class IntentoBloqueado:
    id: str
    cliente_id: str
    operacion: str
    origen: str
    estado_consentimiento: str
    ocurrido_en: datetime
    correlacion_id: str | None


class RepositorioPerfiles:
    def __init__(self, fabrica: sessionmaker[Session]) -> None:
        self._fabrica = fabrica

    def guardar(
        self,
        *,
        cliente_id: str,
        perfil: dict[str, Any],
        completitud: float,
        degradado: bool,
        consentimiento_id: str | None,
        correlacion_id: str | None,
        creado_en: datetime,
        linaje: list[LineaLinaje],
    ) -> PerfilGuardado:
        """Perfil y linaje en la misma transacción: no existe uno sin el otro."""
        perfil_id = str(uuid.uuid4())
        with self._fabrica() as sesion, sesion.begin():
            sesion.add(
                PerfilEnriquecidoDB(
                    id=perfil_id,
                    cliente_id=cliente_id,
                    perfil=perfil,
                    completitud=completitud,
                    degradado=degradado,
                    consentimiento_id=consentimiento_id,
                    correlacion_id=correlacion_id,
                    creado_en=creado_en,
                )
            )
            sesion.flush()
            sesion.add_all(
                LinajeFuenteDB(
                    perfil_id=perfil_id,
                    fuente=linea.fuente,
                    campos=list(linea.campos),
                    obtenido_en=linea.obtenido_en,
                    ttl_s=linea.ttl_s,
                    hit_cache=linea.hit_cache,
                    degradado=linea.degradado,
                    por_defecto=linea.por_defecto,
                    consentimiento_id=consentimiento_id,
                )
                for linea in linaje
            )
        return PerfilGuardado(
            perfil_id, cliente_id, perfil, completitud, degradado, consentimiento_id, creado_en
        )

    def ultimo(self, cliente_id: str) -> PerfilGuardado | None:
        with self._fabrica() as sesion:
            fila = sesion.scalars(
                select(PerfilEnriquecidoDB)
                .where(PerfilEnriquecidoDB.cliente_id == cliente_id)
                .order_by(PerfilEnriquecidoDB.creado_en.desc())
                .limit(1)
            ).first()
            if fila is None:
                return None
            return PerfilGuardado(
                fila.id,
                fila.cliente_id,
                fila.perfil,
                fila.completitud,
                fila.degradado,
                fila.consentimiento_id,
                _utc(fila.creado_en),  # type: ignore[arg-type]
            )

    def linaje_de(self, perfil: PerfilGuardado) -> LinajeGuardado:
        with self._fabrica() as sesion:
            filas = sesion.scalars(
                select(LinajeFuenteDB)
                .where(LinajeFuenteDB.perfil_id == perfil.id)
                .order_by(LinajeFuenteDB.id)
            ).all()
            return LinajeGuardado(
                perfil_id=perfil.id,
                cliente_id=perfil.cliente_id,
                creado_en=perfil.creado_en,
                consentimiento_id=perfil.consentimiento_id,
                fuentes=tuple(
                    LineaLinaje(
                        f.fuente,
                        tuple(f.campos),
                        _utc(f.obtenido_en),
                        f.ttl_s,
                        f.hit_cache,
                        f.degradado,
                        f.por_defecto,
                    )
                    for f in filas
                ),
            )

    def registrar_intento(
        self,
        *,
        cliente_id: str,
        operacion: str,
        origen: str,
        estado_consentimiento: str,
        correlacion_id: str | None,
        ocurrido_en: datetime,
    ) -> None:
        with self._fabrica() as sesion, sesion.begin():
            sesion.add(
                IntentoBloqueadoDB(
                    id=str(uuid.uuid4()),
                    cliente_id=cliente_id,
                    operacion=operacion,
                    origen=origen,
                    estado_consentimiento=estado_consentimiento,
                    ocurrido_en=ocurrido_en,
                    correlacion_id=correlacion_id,
                )
            )

    def intentos_de(self, cliente_id: str) -> list[IntentoBloqueado]:
        with self._fabrica() as sesion:
            filas = sesion.scalars(
                select(IntentoBloqueadoDB)
                .where(IntentoBloqueadoDB.cliente_id == cliente_id)
                .order_by(IntentoBloqueadoDB.ocurrido_en)
            ).all()
            return [
                IntentoBloqueado(
                    f.id,
                    f.cliente_id,
                    f.operacion,
                    f.origen,
                    f.estado_consentimiento,
                    _utc(f.ocurrido_en),  # type: ignore[arg-type]
                    f.correlacion_id,
                )
                for f in filas
            ]
