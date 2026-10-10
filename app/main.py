import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.proveedores.api import router as perfiles_router
from app.proveedores.wiring import construir_servicio


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    servicio = construir_servicio()
    app.state.servicio_perfil = servicio
    sondeador = asyncio.create_task(servicio.ejecutar_sondeador())
    try:
        yield
    finally:
        sondeador.cancel()


app = FastAPI(title="Solventa Backend", version="0.1.0", lifespan=lifespan)
app.include_router(perfiles_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
