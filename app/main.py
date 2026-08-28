"""Entrypoint FastAPI (BQI-60). /health es liveness puro (sin DB, para el
healthcheck de Docker); /status sí refleja el estado real de la BD."""

from fastapi import FastAPI

from app.api.routes import failures as failures_routes
from app.api.routes import pipeline as pipeline_routes
from app.api.routes import status as status_routes
from app.core.sentry import init_sentry

init_sentry("web")

app = FastAPI(title="BQ-Integraciones")

app.include_router(status_routes.router)
app.include_router(failures_routes.router)
app.include_router(pipeline_routes.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
