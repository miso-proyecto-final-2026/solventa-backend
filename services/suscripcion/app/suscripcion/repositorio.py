"""Persistencia de MS Suscripción. Las funciones reciben la sesión: la transacción la maneja quien las llama."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.suscripcion.modelos_db import (
    ESTADOS_QUE_LIBERAN_LA_COTIZACION,
    SCHEMA,
    BaseSuscripcion,
    EventoSalidaDB,
    PolizaDB,
    RevisionAsistidaDB,
    SuscripcionDB,
)


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `suscripcion`. En SQLite (desarrollo y pruebas) no hay esquemas."""
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
    BaseSuscripcion.metadata.create_all(engine)


def _utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


utc = _utc


# --- suscripciones ---------------------------------------------------------------------------------------------

def nueva_suscripcion(
    sesion: Session,
    *,
    id: str,
    cotizacion_id: str,
    cliente_id: str,
    clave_idempotencia: str,
    producto: str,
    moneda: str,
    monto_asegurado: int,
    prima_mensual: Decimal | float,
    prima_anual: Decimal | float,
    ahora: datetime,
    correlacion_id: str | None = None,
    metodo_pago_token: str | None = None,
    periodicidad_pago: str = "ANUAL",
    coberturas: list[dict[str, Any]] | None = None,
    cotizacion_version_reglas: str | None = None,
) -> SuscripcionDB:
    """Crea la saga en `EN_PROGRESO` / `DECISION`. Puede lanzar IntegrityError (clave repetida o cotización ya usada)."""
    suscripcion = SuscripcionDB(
        id=id,
        cotizacion_id=cotizacion_id,
        cliente_id=cliente_id,
        clave_idempotencia=clave_idempotencia,
        estado_saga="EN_PROGRESO",
        paso_actual="DECISION",
        producto=producto,
        moneda=moneda,
        monto_asegurado=monto_asegurado,
        prima_mensual=Decimal(str(prima_mensual)),
        prima_anual=Decimal(str(prima_anual)),
        periodicidad_pago=periodicidad_pago,
        motivos=[],
        correlacion_id=correlacion_id,
        metodo_pago_token=metodo_pago_token,
        coberturas=coberturas,
        cotizacion_version_reglas=cotizacion_version_reglas,
        created_at=ahora,
        updated_at=ahora,
    )
    sesion.add(suscripcion)
    sesion.flush()
    return suscripcion


def obtener(sesion: Session, suscripcion_id: str) -> SuscripcionDB | None:
    return sesion.get(SuscripcionDB, suscripcion_id)


def buscar_por_clave(sesion: Session, cliente_id: str, clave: str) -> SuscripcionDB | None:
    return sesion.execute(
        select(SuscripcionDB).where(SuscripcionDB.cliente_id == cliente_id, SuscripcionDB.clave_idempotencia == clave)
    ).scalar_one_or_none()


def buscar_activa_por_cotizacion(sesion: Session, cotizacion_id: str) -> SuscripcionDB | None:
    return sesion.execute(
        select(SuscripcionDB).where(
            SuscripcionDB.cotizacion_id == cotizacion_id,
            SuscripcionDB.estado_saga.not_in(ESTADOS_QUE_LIBERAN_LA_COTIZACION),
        )
    ).scalar_one_or_none()


def registrar_decision(
    sesion: Session,
    suscripcion: SuscripcionDB,
    *,
    decision: str,
    estado_saga: str,
    paso_actual: str,
    motivos: list[str],
    ajuste: dict[str, Any] | None,
    reglas_version: str,
    ahora: datetime,
) -> SuscripcionDB:
    suscripcion.decision = decision
    suscripcion.estado_saga = estado_saga
    suscripcion.paso_actual = paso_actual
    suscripcion.motivos = list(motivos)
    suscripcion.ajuste = ajuste
    suscripcion.reglas_version = reglas_version
    suscripcion.decidido_en = ahora
    suscripcion.updated_at = ahora
    inicio = _utc(suscripcion.created_at)
    suscripcion.duracion_decision_ms = int((ahora - inicio).total_seconds() * 1000) if inicio else None
    sesion.flush()
    return suscripcion


def pendientes_de_decision(sesion: Session, *, antes_de: datetime, limite: int) -> list[SuscripcionDB]:
    """Sagas sin decidir y creadas antes de `antes_de` (el proceso que las iba a decidir cayó). Bloquea las filas
    con SKIP LOCKED para que dos réplicas no tomen las mismas."""
    return list(
        sesion.execute(
            select(SuscripcionDB)
            .where(SuscripcionDB.decision.is_(None), SuscripcionDB.created_at < antes_de)
            .order_by(SuscripcionDB.created_at)
            .limit(limite)
            .with_for_update(skip_locked=True)
        ).scalars()
    )


# --- revisión asistida -----------------------------------------------------------------------------------------

def crear_revision_asistida(sesion: Session, suscripcion_id: str, motivo: str, ahora: datetime) -> RevisionAsistidaDB:
    revision = RevisionAsistidaDB(suscripcion_id=suscripcion_id, motivo_derivacion=motivo[:200], created_at=ahora)
    sesion.add(revision)
    sesion.flush()
    return revision


def revisiones_de(sesion: Session, suscripcion_id: str) -> list[RevisionAsistidaDB]:
    return list(
        sesion.execute(
            select(RevisionAsistidaDB).where(RevisionAsistidaDB.suscripcion_id == suscripcion_id).order_by(RevisionAsistidaDB.id)
        ).scalars()
    )


# --- pólizas (HU20) ------------------------------------------------------------------------------------------------

def poliza_de_suscripcion(sesion: Session, suscripcion_id: str) -> PolizaDB | None:
    return sesion.execute(select(PolizaDB).where(PolizaDB.suscripcion_id == suscripcion_id)).scalar_one_or_none()


def polizas_de_cliente(sesion: Session, cliente_id: str) -> list[tuple[PolizaDB, SuscripcionDB]]:
    """La billetera del cliente: sus pólizas con la suscripción que les da la prima, las más recientes primero."""
    filas = sesion.execute(
        select(PolizaDB, SuscripcionDB)
        .join(SuscripcionDB, SuscripcionDB.id == PolizaDB.suscripcion_id)
        .where(PolizaDB.cliente_id == cliente_id)
        .order_by(PolizaDB.created_at.desc(), PolizaDB.id)
    )
    return [(p, s) for p, s in filas]


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


def marcar_publicado(salida: EventoSalidaDB, *, payload_redactado: dict[str, Any], ahora: datetime) -> None:
    """Tras publicar se conserva el evento como evidencia, pero sin el token de la tarjeta."""
    salida.payload = payload_redactado
    salida.publicado_en = ahora
    salida.ultimo_error = None


def marcar_fallo(salida: EventoSalidaDB, error: str) -> None:
    salida.intentos += 1
    salida.ultimo_error = error[:500]
