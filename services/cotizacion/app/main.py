import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.cotizacion.api import router as cotizaciones_router
from app.cotizacion.config import ConfigCotizacion
from app.cotizacion.errores import registrar_errores
from app.cotizacion.wiring import construir_servicio_cotizacion
from app.proveedores.api import router as perfiles_router
from app.proveedores.wiring import construir_servicio


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    servicio = construir_servicio()
    app.state.servicio_perfil = servicio
    servicio_cotizacion, engine_cotizacion, escritor = construir_servicio_cotizacion(
        servicio
    )
    escritor.iniciar()
    app.state.servicio_cotizacion = servicio_cotizacion
    app.state.auditor_api_keys = ConfigCotizacion.desde_entorno().auditor_api_keys
    sondeador = asyncio.create_task(servicio.ejecutar_sondeador())
    try:
        yield
    finally:
        sondeador.cancel()
        await escritor.detener()  # escribe la trazabilidad pendiente antes de cerrar
        await servicio.cerrar()
        engine_cotizacion.dispose()


app = FastAPI(title="Solventa — MS Cotización", version="0.1.0", lifespan=lifespan)
app.include_router(perfiles_router)
app.include_router(cotizaciones_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
