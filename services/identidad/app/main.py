import logging

from fastapi import FastAPI, Request

from app.correlacion import MiddlewareCorrelacion
from app.db import DATABASE_URL, Base, engine
from app.logs import configurar_logs
from app.routers.clientes import router as clientes_router
from app.routers.consentimientos import router as consentimientos_router
from app.routers.contactos_cliente import router as contactos_cliente_router
from app.routers.resumenes_cliente import router as resumenes_cliente_router
from app.routers.sesion import router as sesion_router
from app.routers.sesiones_internas import router as sesiones_internas_router
from app.routers.verificaciones import router as verificaciones_router

# En PostgreSQL el esquema `identidad` lo crea Alembic; create_all es solo para SQLite local.
if DATABASE_URL.startswith("sqlite"):
    Base.metadata.create_all(bind=engine)

configurar_logs()
log = logging.getLogger("solventa.identidad")

app = FastAPI(title="Solventa — MS Identidad", version="0.1.0")
app.add_middleware(MiddlewareCorrelacion)
app.include_router(clientes_router)
app.include_router(consentimientos_router)
app.include_router(verificaciones_router)
app.include_router(sesiones_internas_router)
app.include_router(sesion_router)
app.include_router(resumenes_cliente_router)
app.include_router(contactos_cliente_router)


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    # Demostración del CD (HS1): una línea visible al consultar /health a mano. Las sondas de Kubernetes
    # (liveness/readiness, cada pocos segundos por réplica) no se registran para no llenar Loki.
    if not request.headers.get("user-agent", "").startswith("kube-probe"):
        log.info("health_consultado servicio=solventa-identidad", extra={"campos": {"evento": "health_consultado"}})
    return {"status": "ok"}
