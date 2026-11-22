from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.correlacion import MiddlewareCorrelacion
from app.logs import configurar_logs
from app.socios.acceso import publicar_seguridad_bearer
from app.socios.config import ConfigSocios
from app.socios.errores import publicar_openapi_sin_422, registrar_errores
from app.socios.oauth import router as oauth_router
from app.socios.v1.api import router as v1_router
from app.socios.v1.suscripciones import router as suscripciones_router
from app.socios.wiring import construir_ensamblado, publicar

configurar_logs()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ensamblado = construir_ensamblado(ConfigSocios.desde_entorno())
    publicar(app.state, ensamblado)
    try:
        yield
    finally:
        await ensamblado.detener()


app = FastAPI(
    title="Solventa — API de socios",
    version="1.0.0",
    description=(
        "API para socios de distribución (HU32). Autenticación OAuth2 client credentials en `/oauth/token`; "
        "cotización y venta embebida (suscripción, firma y póliza, HU33) en `/partners/v1`. Cada versión se publica bajo su propio prefijo."
    ),
    lifespan=lifespan,
)
app.include_router(oauth_router)
app.include_router(v1_router)
app.include_router(suscripciones_router)
registrar_errores(app)
publicar_openapi_sin_422(app)
publicar_seguridad_bearer(app)
app.add_middleware(MiddlewareCorrelacion)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Sonda interna de Kubernetes (fuera del borde). Los socios usan /partners/v1/status."""
    return {"status": "ok"}
