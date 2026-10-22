import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.perfilamiento.api import router as perfilamiento_router
from app.perfilamiento.config import ConfigPerfilamiento
from app.perfilamiento.errores import registrar_errores
from app.perfilamiento.wiring import construir_enriquecedor, construir_fuentes


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigPerfilamiento.desde_entorno()
    fuentes = construir_fuentes()
    enriquecedor, engine = construir_enriquecedor(fuentes, config)
    app.state.enriquecedor = enriquecedor
    app.state.api_keys = config.api_keys or {}
    sondeador = asyncio.create_task(fuentes.ejecutar_sondeador())
    try:
        yield
    finally:
        sondeador.cancel()
        await fuentes.cerrar()
        engine.dispose()


app = FastAPI(title="Solventa — MS Perfilamiento", version="0.1.0", lifespan=lifespan)
app.include_router(perfilamiento_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
