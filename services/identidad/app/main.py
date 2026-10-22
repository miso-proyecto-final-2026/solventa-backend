import logging
import time

from fastapi import FastAPI, Request

from app.db import DATABASE_URL, Base, engine
from app.routers.clientes import router as clientes_router
from app.routers.consentimientos import router as consentimientos_router
from app.routers.sesiones_internas import router as sesiones_internas_router
from app.routers.verificaciones import router as verificaciones_router

# En PostgreSQL el esquema `identidad` lo crea Alembic; create_all es solo para SQLite local.
if DATABASE_URL.startswith("sqlite"):
    Base.metadata.create_all(bind=engine)

app = FastAPI(title="Solventa — MS Identidad", version="0.1.0")

# Mismo logger que uvicorn para que las líneas salgan en `kubectl logs` sin configurar nada más.
log = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def registrar_llamada(request: Request, call_next):
    """Una línea por llamada (método, ruta, estado y ms); sin cuerpo ni cabeceras. `/health` no se registra."""
    if request.url.path == "/health":
        return await call_next(request)
    inicio = time.perf_counter()
    respuesta = await call_next(request)
    log.info(
        "llamada %s %s -> %s (%.0f ms)",
        request.method,
        request.url.path,
        respuesta.status_code,
        (time.perf_counter() - inicio) * 1000,
    )
    return respuesta

app.include_router(clientes_router)
app.include_router(consentimientos_router)
app.include_router(verificaciones_router)
app.include_router(sesiones_internas_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
