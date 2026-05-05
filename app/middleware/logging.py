"""
STEP 9 — Request Logging Middleware
=====================================
Injects a unique request_id into every request and logs timing/status.
This runs as Starlette middleware (before FastAPI route handlers).

WHY request_id:
  In production with multiple replicas, a single user request might trigger
  multiple log lines (DB query, external API call, response). request_id
  ties them all together so you can `grep request_id=abc123` in Cloud Logging
  and see the full picture.

HOW IT RELATES TO TENANT CONTEXT:
  This middleware sets request_id.
  The auth dependency (middleware/auth.py) later sets brand_id + user_id.
  Together they give complete per-request context in every log line.

TIMING:
  We log total request duration. This is useful for:
    - Detecting slow endpoints
    - Per-tenant performance comparison (is Brand A's sync slower than Brand B's?)
"""
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.logging import get_logger

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Clear any context from previous request (connection reuse)
        clear_contextvars()

        request_id = str(uuid.uuid4())
        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        start_time = time.monotonic()

        logger.info("request_started")

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.error(
                "request_failed",
                duration_ms=round(duration_ms, 2),
                error=str(exc),
                exc_info=True,
            )
            raise

        duration_ms = (time.monotonic() - start_time) * 1000
        logger.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )

        # Propagate request_id back to client for correlation
        response.headers["X-Request-ID"] = request_id
        return response
