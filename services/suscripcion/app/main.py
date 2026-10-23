import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.correlacion import MiddlewareCorrelacion
from app.suscripcion.api import router as suscripciones_router
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.errores import registrar_errores
from app.suscripcion.relay import RelayEventos
from app.suscripcion.wiring import construir_publicador, construir_servicio

log = logging.getLogger("suscripcion")


async def _barrer(servicio, espera_s: float) -> None:
    while True:
        await asyncio.sleep(max(espera_s / 3, 1.0))
        try:
            await servicio.retomar_pendientes(espera_s)
        except Exception:
            log.exception("fallo en el barrido de sagas sin decidir")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigSuscripcion.desde_entorno()
    servicio, engine, clientes = construir_servicio(config)
    publicador = construir_publicador()
    if hasattr(publicador, "iniciar"):
        await publicador.iniciar()
    relay = RelayEventos(sessionmaker(bind=engine, expire_on_commit=False), publicador)
    app.state.servicio_suscripcion = servicio
    app.state.tareas = set()
    segundo_plano = [
        asyncio.create_task(relay.ejecutar()),
        asyncio.create_task(_barrer(servicio, config.espera_barrido_s)),
    ]
    try:
        yield
    finally:
        for t in segundo_plano:
            t.cancel()
        await asyncio.gather(*segundo_plano, return_exceptions=True)
        if hasattr(publicador, "detener"):
            await publicador.detener()
        for c in clientes:
            await c.aclose()
        engine.dispose()


app = FastAPI(title="Solventa — MS Suscripción", version="0.1.0", lifespan=lifespan)
app.include_router(suscripciones_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
