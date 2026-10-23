import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.siniestros.api import router as siniestros_router
from app.siniestros.config import ConfigSiniestros
from app.siniestros.errores import registrar_errores
from app.siniestros.wiring import construir_receptor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigSiniestros.desde_entorno()
    receptor, consumidor, engine = construir_receptor(config)
    app.state.receptor = receptor
    app.state.api_keys = config.api_keys or {}
    # El bus en memoria se vacía al reiniciar: se repone desde la base antes de consumir.
    await receptor.reponer_rezago()
    tarea = asyncio.create_task(consumidor.ejecutar())
    try:
        yield
    finally:
        tarea.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarea
        engine.dispose()


app = FastAPI(title="Solventa — MS Siniestros", version="0.1.0", lifespan=lifespan)
app.include_router(siniestros_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
