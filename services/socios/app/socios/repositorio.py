"""Persistencia de MS Socios. Las funciones reciben la sesión: la transacción la maneja quien las llama."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, create_engine, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.socios.modelos_db import (
    ESTADO_ACTIVO,
    SCHEMA,
    BaseSocios,
    ClienteApiDB,
    ContratoApiDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `socios`. En SQLite (desarrollo y pruebas) no hay esquemas."""
    url = normalizar_url(url)
    if url.startswith("sqlite"):
        memoria = ":memory:" in url or url in ("sqlite://", "sqlite:///")
        engine = create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool if memoria else None
        )
        return engine.execution_options(schema_translate_map={SCHEMA: None})
    return create_engine(url, pool_pre_ping=True)


def crear_tablas_locales(engine: Engine) -> None:
    """Solo para SQLite de desarrollo. En PostgreSQL el esquema lo crea Alembic."""
    BaseSocios.metadata.create_all(engine)


def utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


def buscar_cliente(sesion: Session, client_id: str) -> ClienteApiDB | None:
    return sesion.execute(select(ClienteApiDB).where(ClienteApiDB.client_id == client_id)).scalar_one_or_none()


def contrato_vigente(sesion: Session, client_id: str, version: str, ahora: datetime) -> ContratoApiDB | None:
    """El contrato más reciente ya iniciado y no vencido de un socio activo para esa versión del API."""
    return sesion.execute(
        select(ContratoApiDB)
        .join(ClienteApiDB, ClienteApiDB.id == ContratoApiDB.cliente_api_id)
        .where(
            ClienteApiDB.client_id == client_id,
            ClienteApiDB.estado == ESTADO_ACTIVO,
            ContratoApiDB.version == version,
            ContratoApiDB.fecha_inicio <= ahora,
            or_(ContratoApiDB.fecha_fin.is_(None), ContratoApiDB.fecha_fin > ahora),
        )
        .order_by(ContratoApiDB.fecha_inicio.desc())
        .limit(1)
    ).scalar_one_or_none()


def guardar_socio(
    sesion: Session,
    *,
    client_id: str,
    nombre_socio: str,
    client_secret_hash: str,
    version: str,
    endpoints: Iterable[str],
    cuota_rps: int,
    ahora: datetime,
) -> ClienteApiDB:
    """Alta o actualización idempotente de un socio y su contrato vigente (lo usa el script de siembra).

    Si el socio ya existe, se reemplaza el hash del secreto y se actualiza el contrato de esa versión en lugar de crear
    otro: sembrar dos veces deja el mismo estado.
    """
    cliente = buscar_cliente(sesion, client_id)
    if cliente is None:
        cliente = ClienteApiDB(
            id=str(uuid4()),
            client_id=client_id,
            nombre_socio=nombre_socio,
            client_secret_hash=client_secret_hash,
            estado=ESTADO_ACTIVO,
            creado_en=ahora,
            actualizado_en=ahora,
        )
        sesion.add(cliente)
        sesion.flush()
    else:
        cliente.nombre_socio = nombre_socio
        cliente.client_secret_hash = client_secret_hash
        cliente.estado = ESTADO_ACTIVO
        cliente.actualizado_en = ahora

    contrato = sesion.execute(
        select(ContratoApiDB).where(ContratoApiDB.cliente_api_id == cliente.id, ContratoApiDB.version == version)
    ).scalars().first()
    if contrato is None:
        sesion.add(
            ContratoApiDB(
                id=str(uuid4()),
                cliente_api_id=cliente.id,
                version=version,
                endpoints_habilitados=sorted(set(endpoints)),
                cuota_rps=cuota_rps,
                fecha_inicio=ahora,
                fecha_fin=None,
                creado_en=ahora,
            )
        )
    else:
        contrato.endpoints_habilitados = sorted(set(endpoints))
        contrato.cuota_rps = cuota_rps
        contrato.fecha_fin = None
    return cliente
