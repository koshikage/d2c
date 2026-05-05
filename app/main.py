"""
FastAPI Application Factory
=============================
Creates and configures the FastAPI app instance.

LIFESPAN (startup/shutdown):
  FastAPI 0.93+ uses @asynccontextmanager lifespan instead of
  @app.on_event("startup"). This is cleaner: setup code before yield,
  teardown after yield — like a pytest fixture.

  On startup: configure logging, log app version and environment.
  On shutdown: dispose DB connection pool (clean TCP FIN to Postgres).

MIDDLEWARE ORDER (matters!):
  Middleware is applied in reverse registration order (last registered = outermost).
  Our stack, outside-in:
    1. CORSMiddleware (handles preflight before anything else)
    2. RequestLoggingMiddleware (logs every request, sets request_id)
    3. FastAPI routing + dependencies (JWT auth, DB session, etc.)

WHY SEPARATE APPLICATION FACTORY:
  The `create_app()` function makes the app importable without side effects.
  Tests can call create_app() to get a test instance without starting a server.

PROS:
  + Clean startup/shutdown via lifespan
  + Testable: create_app() returns a fresh instance per test
  + Middleware order is explicit and documented

CONS:
  - Lifespan pattern is less familiar than @app.on_event for some developers
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.session import async_engine, check_db_health
from app.middleware.logging import RequestLoggingMiddleware

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: runs setup before yield, teardown after yield.
    """
    # ── Startup ──────────────────────────────────────────────────────────────
    setup_logging()
    logger.info(
        "app_starting",
        name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )

    db_healthy = await check_db_health()
    if not db_healthy:
        logger.warning("startup_db_not_ready")
    else:
        logger.info("startup_db_ok")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("app_shutting_down")
    await async_engine.dispose()
    logger.info("app_shutdown_complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="""
D2C Platform API — Multi-tenant marketing data platform.

Connect your Shopify store and Meta Ads account, sync your data,
and query spend vs revenue analytics — all tenant-isolated.
        """,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── Middleware (registered in reverse order — last = outermost) ───────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(api_router)

    # ── Health check (no auth required) ──────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health():
        db_ok = await check_db_health()
        return {
            "status": "healthy" if db_ok else "degraded",
            "db": "ok" if db_ok else "error",
            "version": settings.APP_VERSION,
        }

    return app


app = create_app()
