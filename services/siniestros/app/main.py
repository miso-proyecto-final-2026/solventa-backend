from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.siniestros.api import router as siniestros_router
from app.siniestros.config import ConfigSiniestros
from app.siniestros.errores import registrar_errores
from app.siniestros.wiring import construir_ensamblado


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigSiniestros.desde_entorno()
    ensamblado = construir_ensamblado(config)
    app.state.receptor = ensamblado.receptor
    app.state.api_keys = config.api_keys or {}
    await ensamblado.iniciar()
    try:
        yield
    finally:
        await ensamblado.detener()


app = FastAPI(title="Solventa — MS Siniestros", version="0.1.0", lifespan=lifespan)
app.include_router(siniestros_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
