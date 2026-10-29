from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.logs import configurar_logs
from app.pagos.acceso import construir_verificador
from app.pagos.api import router as pagos_router
from app.pagos.api_gestion import router as gestion_router
from app.pagos.config import ConfigPagos
from app.pagos.errores import registrar_errores
from app.pagos.wiring import construir_ensamblado

configurar_logs()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigPagos.desde_entorno()
    ensamblado = construir_ensamblado(config)
    app.state.servicio_cobros = ensamblado.servicio
    app.state.servicio_gestion = ensamblado.gestion
    app.state.verificador_gestion = construir_verificador(config)
    app.state.api_keys = config.api_keys or {}
    await ensamblado.iniciar()
    try:
        yield
    finally:
        await ensamblado.detener()


app = FastAPI(title="Solventa — MS Pagos", version="0.1.0", lifespan=lifespan)
app.include_router(pagos_router)
app.include_router(gestion_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
