from fastapi import FastAPI

from app.db import Base, engine
from app.routers.clientes import router as clientes_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Solventa Backend", version="0.1.0")
app.include_router(clientes_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
