"""Persistencia de MS Notificaciones. Las funciones reciben la sesión: la transacción la maneja quien las llama."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.notificaciones.dominio import IDIOMA_POR_DEFECTO, EstadoEnvio
from app.notificaciones.modelos_db import (
    SCHEMA,
    BandejaPushDB,
    BaseNotificaciones,
    DispositivoDB,
    EnvioDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `notificaciones`. En SQLite (desarrollo y pruebas) no hay esquemas."""
    url = normalizar_url(url)
    if url.startswith("sqlite"):
        memoria = ":memory:" in url or url in ("sqlite://", "sqlite:///")
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if memoria else None,
        )

        # Receta de SQLAlchemy para que SQLite respete transacciones y savepoints como PostgreSQL (el controlador de
        # Python no lo hace); la idempotencia usa savepoints (misma receta que las pruebas de la plantilla de eventos).
        @event.listens_for(engine, "connect")
        def _sin_autotransaccion(conexion_dbapi, _registro):
            conexion_dbapi.isolation_level = None

        @event.listens_for(engine, "begin")
        def _begin_explicito(conexion):
            conexion.exec_driver_sql("BEGIN")

        return engine.execution_options(schema_translate_map={SCHEMA: None})
    return create_engine(url, pool_pre_ping=True)


def crear_tablas_locales(engine: Engine) -> None:
    """Solo para SQLite de desarrollo. En PostgreSQL el esquema lo crea Alembic."""
    BaseNotificaciones.metadata.create_all(engine)


def utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


# --- dispositivos ----------------------------------------------------------------------------------------------------

def registrar_dispositivo(sesion: Session, cliente_id: str, dispositivo_id: str, idioma: str, ahora: datetime) -> DispositivoDB:
    """Crea el dispositivo o actualiza su idioma. Idempotente: registrar dos veces el mismo no crea otro."""
    existente = sesion.execute(
        select(DispositivoDB).where(DispositivoDB.cliente_id == cliente_id, DispositivoDB.dispositivo_id == dispositivo_id)
    ).scalar_one_or_none()
    if existente is not None:
        existente.idioma, existente.actualizado_en = idioma, ahora
        return existente
    nuevo = DispositivoDB(
        id=str(uuid4()), cliente_id=cliente_id, dispositivo_id=dispositivo_id, idioma=idioma, creado_en=ahora, actualizado_en=ahora
    )
    try:
        with sesion.begin_nested():
            sesion.add(nuevo)
    except IntegrityError:  # otra réplica lo registró primero
        existente = sesion.execute(
            select(DispositivoDB).where(DispositivoDB.cliente_id == cliente_id, DispositivoDB.dispositivo_id == dispositivo_id)
        ).scalar_one()
        existente.idioma, existente.actualizado_en = idioma, ahora
        return existente
    return nuevo


def idioma_de(sesion: Session, cliente_id: str) -> str:
    """El idioma del dispositivo que el cliente usó por última vez; sin dispositivos, el por defecto."""
    idioma = sesion.execute(
        select(DispositivoDB.idioma)
        .where(DispositivoDB.cliente_id == cliente_id)
        .order_by(DispositivoDB.actualizado_en.desc())
        .limit(1)
    ).scalar_one_or_none()
    return idioma or IDIOMA_POR_DEFECTO


# --- envíos ----------------------------------------------------------------------------------------------------------

def crear_envio(sesion: Session, envio: EnvioDB) -> bool:
    """False si ya existía un envío para ese evento y canal (la restricción única lo decide, también entre réplicas)."""
    try:
        with sesion.begin_nested():
            sesion.add(envio)
        return True
    except IntegrityError:
        return False


def envios_fallidos(sesion: Session, limite: int) -> list[EnvioDB]:
    return list(
        sesion.execute(
            select(EnvioDB)
            .where(EnvioDB.estado == EstadoEnvio.FALLIDO.value)
            .order_by(EnvioDB.creado_en.desc())
            .limit(limite)
        ).scalars()
    )


def envios_de_evento(sesion: Session, evento_origen_id: str) -> list[EnvioDB]:
    return list(sesion.execute(select(EnvioDB).where(EnvioDB.evento_origen_id == evento_origen_id).order_by(EnvioDB.canal)).scalars())


# --- bandeja del push simulado ---------------------------------------------------------------------------------------

def bandeja_de(sesion: Session, cliente_id: str, *, solo_nuevas: bool, limite: int) -> list[BandejaPushDB]:
    """Las «nuevas» son lo que la app aún no mostró y se creó desde que el cliente registró su primer dispositivo.

    Como en un push real, un dispositivo no recibe lo anterior a su existencia: sin esto, abrir la app la primera vez
    lanzaría de golpe los avisos de pólizas viejas. Sin dispositivo registrado no hay nada que entregar. El historial
    (`solo_nuevas=False`) devuelve todo.
    """
    consulta = select(BandejaPushDB).where(BandejaPushDB.cliente_id == cliente_id)
    if solo_nuevas:
        primer_dispositivo = select(func.min(DispositivoDB.creado_en)).where(DispositivoDB.cliente_id == cliente_id).scalar_subquery()
        consulta = consulta.where(BandejaPushDB.recibida_en.is_(None), BandejaPushDB.creado_en >= primer_dispositivo)
    return list(sesion.execute(consulta.order_by(BandejaPushDB.creado_en.desc()).limit(limite)).scalars())


def marcar_recibida(sesion: Session, cliente_id: str, notificacion_id: str, ahora: datetime) -> bool:
    """False si no existe o es de otro cliente. Marcarla otra vez conserva la primera fecha."""
    fila = sesion.get(BandejaPushDB, notificacion_id)
    if fila is None or fila.cliente_id != cliente_id:
        return False
    if fila.recibida_en is None:
        fila.recibida_en = ahora
    return True
