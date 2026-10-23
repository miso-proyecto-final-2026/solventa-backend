from fastapi import FastAPI

from app.db import DATABASE_URL, Base, engine
from app.routers.clientes import router as clientes_router
from app.routers.consentimientos import router as consentimientos_router
from app.routers.sesion import router as sesion_router
from app.routers.sesiones_internas import router as sesiones_internas_router
from app.routers.verificaciones import router as verificaciones_router

# En PostgreSQL el esquema `identidad` lo crea Alembic; create_all es solo para SQLite local.
if DATABASE_URL.startswith("sqlite"):
    Base.metadata.create_all(bind=engine)

app = FastAPI(title="Solventa — MS Identidad", version="0.1.0")
app.include_router(clientes_router)
app.include_router(consentimientos_router)
app.include_router(verificaciones_router)
app.include_router(sesiones_internas_router)
app.include_router(sesion_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
