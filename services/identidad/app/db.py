import os

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

SCHEMA = "identidad"
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./solventa.db")


def normalizar_url(url: str) -> str:
    """Usa el driver psycopg 3 para las URL de PostgreSQL sin driver explícito."""
    for prefijo in ("postgresql://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg://" + url[len(prefijo) :]
    return url


def crear_engine(url: str) -> Engine:
    """Engine del esquema `identidad` (ADR-0001). SQLite (dev/pruebas) no tiene esquemas:
    se traduce el esquema a ninguno."""
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


engine = crear_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    # Esquema propio: el contexto Identidad no comparte tablas ni llaves foráneas con otros.
    metadata = MetaData(schema=SCHEMA)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
