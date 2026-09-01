"""Entrypoint FastAPI (BQI-60). /health es liveness puro (sin DB, para el
healthcheck de Docker); /status sí refleja el estado real de la BD."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import failures as failures_routes
from app.api.routes import pipeline as pipeline_routes
from app.api.routes import status as status_routes
from app.core.config import settings
from app.core.sentry import init_sentry

init_sentry("web")

if settings.is_production and not settings.API_KEY:
    # Fallar temprano: mejor que el proceso ni arranque a que quede una API
    # administrativa (pipeline on/off, retry) expuesta sin autenticación.
    raise RuntimeError(
        "API_KEY vacía en producción: la API quedaría sin autenticación. "
        "Definir API_KEY antes de arrancar."
    )

app = FastAPI(title="BQ-Integraciones")

app.include_router(status_routes.router)
app.include_router(failures_routes.router)
app.include_router(pipeline_routes.router)

# ── Seguridad: API Key (BQI-64) ──────────────────────────────────────────
_API_KEY_EXEMPT = {"/health", "/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """
    Verifica el header X-API-Key en todos los endpoints excepto los exentos.
    Si API_KEY está vacío en .env, la verificación se omite (desarrollo
    local sin key configurada) — mismo patrón que Stock-Service.
    """
    if settings.API_KEY and request.url.path not in _API_KEY_EXEMPT:
        if request.headers.get("X-API-Key", "") != settings.API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "API Key inválida o ausente — incluir header X-API-Key"},
            )
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
