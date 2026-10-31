import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.correlacion import MiddlewareCorrelacion
from app.logs import configurar_logs
from app.suscripcion.acceso import construir_verificador
from app.suscripcion.api import router as suscripciones_router
from app.suscripcion.api_asistida import router as asistida_router
from app.suscripcion.api_firma import router as firma_router
from app.suscripcion.api_polizas import router as polizas_router
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.errores import registrar_errores
from app.suscripcion.relay import RelayEventos
from app.suscripcion.wiring import (
    construir_consumidor_cobros,
    construir_generador,
    construir_publicador,
    construir_servicio,
    construir_servicio_asistida,
    construir_servicio_firma,
)

configurar_logs()
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
    # Resultados del cobro de MS Pagos (HU18): avanzan la saga a COBRO_REALIZADO o registran el rechazo.
    consumidor = construir_consumidor_cobros(engine, publicador)
    # Genera el PDF de las pólizas registradas por el consumidor del cobro (HU20), sin bloquear el pago.
    generador = construir_generador(config, engine, clientes)
    # Firma de la póliza (HU21): el servicio atiende la API y su barrido compensa las firmas vencidas y emite las firmadas.
    firma = construir_servicio_firma(config, engine, clientes)
    app.state.servicio_suscripcion = servicio
    app.state.servicio_firma = firma
    app.state.servicio_asistida = construir_servicio_asistida(servicio, firma, engine, clientes, config)
    app.state.verificador_asesor = construir_verificador(config)
    app.state.tareas = set()
    segundo_plano = [
        asyncio.create_task(relay.ejecutar()),
        asyncio.create_task(_barrer(servicio, config.espera_barrido_s)),
        asyncio.create_task(generador.ejecutar()),
        asyncio.create_task(firma.ejecutar(config.firma_barrido_s)),
    ]
    if consumidor is not None:
        await consumidor.iniciar()
        segundo_plano.append(asyncio.create_task(consumidor.ejecutar()))
    try:
        yield
    finally:
        if consumidor is not None:
            await consumidor.detener()
        for t in segundo_plano:
            t.cancel()
        await asyncio.gather(*segundo_plano, return_exceptions=True)
        if hasattr(publicador, "detener"):
            await publicador.detener()
        for c in clientes:
            await c.aclose()
        engine.dispose()


app = FastAPI(title="Solventa — MS Suscripción", version="0.1.0", lifespan=lifespan)
# Primero: si no, `GET /v1/suscripciones/asistidas` coincidiría con `/{suscripcion_id}`.
app.include_router(asistida_router)
app.include_router(suscripciones_router)
app.include_router(polizas_router)
app.include_router(firma_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
