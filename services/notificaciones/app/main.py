import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.correlacion import MiddlewareCorrelacion
from app.logs import configurar_logs
from app.notificaciones.acceso import construir_verificador
from app.notificaciones.api import router as notificaciones_router
from app.notificaciones.config import ConfigNotificaciones
from app.notificaciones.errores import registrar_errores
from app.notificaciones.wiring import construir_ensamblado

configurar_logs()
log = logging.getLogger("solventa.notificaciones")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = ConfigNotificaciones.desde_entorno()
    ensamblado = construir_ensamblado(config)
    app.state.servicio_notificaciones = ensamblado.servicio
    app.state.identidad = ensamblado.identidad
    app.state.fabrica = ensamblado.fabrica
    app.state.verificador_operador = construir_verificador(config)
    await ensamblado.iniciar()
    try:
        yield
    finally:
        await ensamblado.detener()


app = FastAPI(title="Solventa — MS Notificaciones", version="0.1.0", lifespan=lifespan)
app.include_router(notificaciones_router)
registrar_errores(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    # Demostración del CD (HS1): una línea visible al consultar /health a mano. Las sondas de Kubernetes
    # (liveness/readiness, cada pocos segundos por réplica) no se registran para no llenar Loki.
    if not request.headers.get("user-agent", "").startswith("kube-probe"):
        log.info("health_consultado servicio=solventa-notificaciones", extra={"campos": {"evento": "health_consultado"}})
    return {"status": "ok"}
