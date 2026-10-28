"""Persistencia de MS Pagos. Las funciones reciben la sesión: la transacción la maneja quien las llama."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.pagos.dominio import ESTADOS_CASO_ABIERTOS
from app.pagos.modelos_db import (
    SCHEMA,
    BasePagos,
    CasoCobroFallidoDB,
    EventoSalidaDB,
    IntentoCobroDB,
    NotaCobroFallidoDB,
    TokenTarjetaDB,
    TransaccionDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `pagos`. En SQLite (desarrollo y pruebas) no hay esquemas."""
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
    BasePagos.metadata.create_all(engine)


def utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


# --- tokens ----------------------------------------------------------------------------------------------------

def token_por_valor(sesion: Session, token: str) -> TokenTarjetaDB | None:
    return sesion.execute(select(TokenTarjetaDB).where(TokenTarjetaDB.token == token)).scalar_one_or_none()


def obtener_o_crear_token(sesion: Session, token: str, cliente_id: str | None, ahora: datetime) -> TokenTarjetaDB:
    """Un token se registra una sola vez, también si dos réplicas lo registran a la vez."""
    existente = token_por_valor(sesion, token)
    if existente is not None:
        return existente
    try:
        with sesion.begin_nested():
            nuevo = TokenTarjetaDB(token=token, cliente_id=cliente_id, created_at=ahora)
            sesion.add(nuevo)
        return nuevo
    except IntegrityError:
        return token_por_valor(sesion, token)


# --- transacciones ---------------------------------------------------------------------------------------------

def obtener(sesion: Session, transaccion_id: str, *, bloquear: bool = False) -> TransaccionDB | None:
    return sesion.get(TransaccionDB, transaccion_id, with_for_update=bloquear or None)


def por_clave(sesion: Session, idempotency_key: str) -> TransaccionDB | None:
    return sesion.execute(
        select(TransaccionDB).where(TransaccionDB.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def insertar_transaccion(sesion: Session, transaccion: TransaccionDB) -> bool:
    """False si otra réplica ya creó una transacción con la misma clave (la restricción única lo decide)."""
    try:
        with sesion.begin_nested():
            sesion.add(transaccion)
        return True
    except IntegrityError:
        return False  # al deshacer el savepoint, la sesión ya descartó el objeto


def contar_reentrega(sesion: Session, transaccion: TransaccionDB, ahora: datetime) -> TransaccionDB:
    """Suma la reentrega en la base (no en memoria): con varias réplicas a la vez, ninguna se pierde. Devuelve la
    transacción releída, con el estado que tenga en este momento."""
    sesion.execute(
        update(TransaccionDB)
        .where(TransaccionDB.id == transaccion.id)
        .values(reentregas=TransaccionDB.reentregas + 1, updated_at=ahora)
    )
    sesion.refresh(transaccion)
    return transaccion


def tomar_pendiente(sesion: Session, transaccion_id: str, ahora: datetime) -> TransaccionDB | None:
    """La transacción, si está PENDIENTE, ya le toca y ninguna otra réplica la tiene bloqueada (SKIP LOCKED)."""
    return sesion.execute(
        select(TransaccionDB)
        .where(
            TransaccionDB.id == transaccion_id,
            TransaccionDB.estado == "PENDIENTE",
            TransaccionDB.proximo_intento_en <= ahora,
        )
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def pendientes_vencidas(sesion: Session, ahora: datetime, limite: int) -> list[str]:
    return list(
        sesion.execute(
            select(TransaccionDB.id)
            .where(TransaccionDB.estado == "PENDIENTE", TransaccionDB.proximo_intento_en <= ahora)
            .order_by(TransaccionDB.proximo_intento_en)
            .limit(limite)
        ).scalars()
    )


def token_de(sesion: Session, transaccion: TransaccionDB) -> TokenTarjetaDB:
    return sesion.get(TokenTarjetaDB, transaccion.token_id)


# --- intentos --------------------------------------------------------------------------------------------------

def nuevo_intento(sesion: Session, transaccion: TransaccionDB, ahora: datetime) -> IntentoCobroDB:
    transaccion.intentos += 1
    intento = IntentoCobroDB(
        transaccion_id=transaccion.id, numero=transaccion.intentos, resultado="EN_CURSO", created_at=ahora, updated_at=ahora
    )
    sesion.add(intento)
    sesion.flush()
    return intento


def intentos_de(sesion: Session, transaccion_id: str) -> list[IntentoCobroDB]:
    return list(
        sesion.execute(
            select(IntentoCobroDB).where(IntentoCobroDB.transaccion_id == transaccion_id).order_by(IntentoCobroDB.numero)
        ).scalars()
    )


# --- cobros fallidos (HU19) ------------------------------------------------------------------------------------

def caso(sesion: Session, caso_id: str, *, bloquear: bool = False) -> CasoCobroFallidoDB | None:
    return sesion.get(CasoCobroFallidoDB, caso_id, with_for_update=bloquear or None)


def caso_abierto_de(sesion: Session, suscripcion_id: str) -> CasoCobroFallidoDB | None:
    """El caso sin recuperar de la suscripción, bloqueado: lo actualiza el cierre de un reintento."""
    return sesion.execute(
        select(CasoCobroFallidoDB)
        .where(CasoCobroFallidoDB.suscripcion_id == suscripcion_id, CasoCobroFallidoDB.estado.in_(ESTADOS_CASO_ABIERTOS))
        .with_for_update()
    ).scalar_one_or_none()


def buscar_casos(
    sesion: Session, *, estados: list[str], gracia_antes_de: datetime | None, desplazamiento: int, limite: int
) -> tuple[list[CasoCobroFallidoDB], int]:
    """Página de casos (los más urgentes primero) y el total que cumple el filtro."""
    filtros = [CasoCobroFallidoDB.estado.in_(estados)]
    if gracia_antes_de is not None:
        filtros.append(CasoCobroFallidoDB.gracia_hasta < gracia_antes_de)
    total = sesion.execute(select(func.count()).select_from(CasoCobroFallidoDB).where(*filtros)).scalar_one()
    pagina = sesion.execute(
        select(CasoCobroFallidoDB)
        .where(*filtros)
        .order_by(CasoCobroFallidoDB.gracia_hasta, CasoCobroFallidoDB.id)
        .offset(desplazamiento)
        .limit(limite)
    ).scalars()
    return list(pagina), total


def tokens_por_id(sesion: Session, ids: set[int]) -> dict[int, TokenTarjetaDB]:
    if not ids:
        return {}
    return {t.id: t for t in sesion.execute(select(TokenTarjetaDB).where(TokenTarjetaDB.id.in_(ids))).scalars()}


def casos_con_gracia_vencida(sesion: Session, ahora: datetime, limite: int) -> list[CasoCobroFallidoDB]:
    """Casos pendientes cuya gracia ya terminó; SKIP LOCKED para que dos réplicas no avisen dos veces."""
    return list(
        sesion.execute(
            select(CasoCobroFallidoDB)
            .where(CasoCobroFallidoDB.estado == "PENDIENTE", CasoCobroFallidoDB.gracia_hasta <= ahora)
            .order_by(CasoCobroFallidoDB.gracia_hasta)
            .limit(limite)
            .with_for_update(skip_locked=True)
        ).scalars()
    )


def transacciones_de(sesion: Session, suscripcion_id: str) -> list[TransaccionDB]:
    return list(
        sesion.execute(
            select(TransaccionDB)
            .where(TransaccionDB.suscripcion_id == suscripcion_id)
            # A igual hora, el cobro original (`cobro_prima:<id>`) antes que sus reintentos (`cobro_prima:<id>:<n>`).
            .order_by(TransaccionDB.created_at, TransaccionDB.idempotency_key)
        ).scalars()
    )


def notas_de(sesion: Session, caso_id: str) -> list[NotaCobroFallidoDB]:
    return list(
        sesion.execute(
            select(NotaCobroFallidoDB)
            .where(NotaCobroFallidoDB.caso_id == caso_id)
            .order_by(NotaCobroFallidoDB.created_at, NotaCobroFallidoDB.id)
        ).scalars()
    )


# --- outbox ----------------------------------------------------------------------------------------------------

def encolar_evento(
    sesion: Session, *, event_id: str, topic: str, clave: str, payload: dict[str, Any], ahora: datetime
) -> EventoSalidaDB:
    salida = EventoSalidaDB(event_id=event_id, topic=topic, clave=clave, payload=payload, creado_en=ahora, intentos=0)
    sesion.add(salida)
    sesion.flush()
    return salida


def salidas_pendientes(sesion: Session, *, limite: int) -> list[EventoSalidaDB]:
    return list(
        sesion.execute(
            select(EventoSalidaDB)
            .where(EventoSalidaDB.publicado_en.is_(None))
            .order_by(EventoSalidaDB.id)
            .limit(limite)
            .with_for_update(skip_locked=True)
        ).scalars()
    )


def marcar_publicado(salida: EventoSalidaDB, ahora: datetime) -> None:
    salida.publicado_en = ahora
    salida.ultimo_error = None


def marcar_fallo(salida: EventoSalidaDB, error: str) -> None:
    salida.intentos += 1
    salida.ultimo_error = error[:500]
