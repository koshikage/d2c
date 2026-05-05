"""
Logging Setup — stdout + rotating log file, per-tenant context
===============================================================

OUTPUT DESTINATIONS:
  Every log line goes to TWO places simultaneously:

  1. stdout   — always on. Docker / Cloud Run reads this stream.
                Locally: your terminal. In GCP: Cloud Logging ingests it.

  2. log file — only when LOG_FILE_ENABLED=true (default: true locally,
                false in production because Cloud Run has no persistent disk).
                File: logs/app.log (or whatever LOG_FILE_PATH is set to).
                Auto-rotates at LOG_FILE_MAX_BYTES (default 10MB).
                Keeps LOG_FILE_BACKUP_COUNT old files (default 5).
                So you always have up to 60MB of recent history on disk.

FILE FORMAT:
  The file always writes JSON regardless of LOG_FORMAT.
  Reason: log files are meant to be parsed by tools (grep, jq, Loki, Datadog).
  Text format is only for human eyes in the terminal.

  stdout format is controlled by LOG_FORMAT:
    text  → coloured, human-readable (local dev)
    json  → machine-parseable (production / CI)

LOG ROTATION:
  Uses Python's RotatingFileHandler.
  When app.log reaches 10MB it is renamed to app.log.1.
  When app.log.1 exists and app.log fills again, app.log.1 → app.log.2, etc.
  After 5 backups the oldest is deleted.
  So on disk you always have at most: app.log + app.log.1..5 = 60MB max.

  WHY NOT TimedRotatingFileHandler:
    Size-based is simpler for a web API — a burst of errors fills a file
    faster than time passes. Size rotation is more predictable.

PER-TENANT CONTEXT:
  middleware/logging.py calls:
      bind_contextvars(request_id=..., method=..., path=...)
  middleware/auth.py calls:
      bind_contextvars(brand_id=..., user_id=..., user_email=...)

  structlog's merge_contextvars processor injects all of these into EVERY
  log line automatically. You never pass them manually.

  Result — every file log line looks like:
  {
    "event": "shopify_rate_limited",
    "wait": 2.0,
    "request_id": "abc-123",
    "brand_id": "uuid-of-brand",
    "user_id": "uuid-of-user",
    "method": "POST",
    "path": "/api/v1/integrations/shopify/sync",
    "level": "warning",
    "timestamp": "2024-01-15T10:30:00.123456Z",
    "logger": "app.integrations.shopify.service"
  }

ADDING NEW LOG CONFIG:
  1. Add the field to Settings in config.py (LOG_FILE_PATH, LOG_FILE_MAX_BYTES, etc.)
  2. Add it to .env.example
  3. Read it here in setup_logging()
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import merge_contextvars


def _make_file_handler(
    log_path: str,
    max_bytes: int,
    backup_count: int,
) -> logging.handlers.RotatingFileHandler:
    """
    Build a RotatingFileHandler that writes JSON to the log file.

    Creates the parent directory if it doesn't exist.
    Uses delay=True so the file is not created until the first log line is
    written — avoids empty log files on startup if no events fire immediately.
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        filename=str(path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,        # don't create the file until the first write
    )

    # The file always gets JSON, regardless of LOG_FORMAT
    # This formatter is a plain string formatter — structlog renders the
    # JSON string before it reaches the stdlib handler, so this just passes
    # the pre-rendered string through unchanged.
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _make_stdout_handler(log_format: str) -> logging.StreamHandler:
    """
    Build a StreamHandler for stdout.
    Format is controlled by LOG_FORMAT: "json" or "text".
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def setup_logging() -> None:
    """
    Configure structlog + stdlib logging with dual output (stdout + file).

    Called ONCE in the FastAPI lifespan (main.py) before any requests are served.
    Calling it more than once is safe — subsequent calls reconfigure in place.

    Reads all config from settings:
      LOG_LEVEL         INFO | DEBUG | WARNING | ERROR | CRITICAL
      LOG_FORMAT        text | json
      LOG_FILE_ENABLED  true | false
      LOG_FILE_PATH     path to the log file (e.g. logs/app.log)
      LOG_FILE_MAX_BYTES    max file size before rotation (default 10MB)
      LOG_FILE_BACKUP_COUNT number of old files to keep (default 5)
    """
    # Import here to avoid circular import (config imports logging, logging imports config)
    from app.core.config import settings

    log_level_int = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # ── structlog processor chain ─────────────────────────────────────────────
    # These run on every log call, in order, before the message is rendered.

    shared_processors: list[Any] = [
        merge_contextvars,                     # inject request_id, brand_id, user_id, etc.
        structlog.stdlib.add_log_level,        # add "level": "info"
        structlog.stdlib.add_logger_name,      # add "logger": "app.integrations.shopify.service"
        structlog.processors.TimeStamper(fmt="iso"),          # add ISO 8601 timestamp
        structlog.processors.StackInfoRenderer(),             # attach stack_info if present
        structlog.processors.ExceptionRenderer(),             # format exceptions inline
    ]

    # ── Handlers ──────────────────────────────────────────────────────────────
    handlers: list[logging.Handler] = []

    # Stdout handler — always on
    stdout_handler = _make_stdout_handler(settings.LOG_FORMAT)
    stdout_handler.setLevel(log_level_int)
    handlers.append(stdout_handler)

    # File handler — on when LOG_FILE_ENABLED=true
    if settings.LOG_FILE_ENABLED:
        file_handler = _make_file_handler(
            log_path=settings.LOG_FILE_PATH,
            max_bytes=settings.LOG_FILE_MAX_BYTES,
            backup_count=settings.LOG_FILE_BACKUP_COUNT,
        )
        file_handler.setLevel(log_level_int)
        handlers.append(file_handler)

        # Log to stdout that we are also logging to a file (useful on startup)
        print(
            f"[logging] writing to file: {settings.LOG_FILE_PATH} "
            f"(max {settings.LOG_FILE_MAX_BYTES // 1024 // 1024}MB × {settings.LOG_FILE_BACKUP_COUNT} backups)",
            file=sys.stdout,
        )

    # ── Configure structlog ───────────────────────────────────────────────────
    # We use two renderer paths:
    #   stdout → text (human) or json (machine) depending on LOG_FORMAT
    #   file   → always json
    #
    # structlog doesn't natively support "two outputs with different formats".
    # The trick: structlog renders to a string, then Python's logging system
    # routes that string to multiple handlers. We make structlog produce JSON
    # always, and if LOG_FORMAT=text we attach a ConsoleRenderer to the
    # stdout handler separately.

    if settings.LOG_FORMAT == "text":
        # For text output on stdout: use ConsoleRenderer
        # For file output (JSON): structlog produces a dict that the stdlib
        # FileHandler receives as a pre-rendered JSON string via a bridge.
        structlog.configure(
            processors=shared_processors + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        # ProcessorFormatter for stdout: text
        text_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
        )
        # ProcessorFormatter for file: JSON
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )

        stdout_handler.setFormatter(text_formatter)
        if settings.LOG_FILE_ENABLED and len(handlers) > 1:
            handlers[1].setFormatter(json_formatter)

    else:
        # JSON mode: both stdout and file get JSON
        structlog.configure(
            processors=shared_processors + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        json_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        for h in handlers:
            h.setFormatter(json_formatter)

    # ── Root logger ───────────────────────────────────────────────────────────
    # Attach all handlers to the root logger so that:
    #   - structlog output (our app code) reaches both stdout and file
    #   - stdlib output (SQLAlchemy, uvicorn, httpx) also reaches both
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level_int)

    # Remove any handlers that were auto-added before setup_logging() was called
    root_logger.handlers.clear()

    for h in handlers:
        root_logger.addHandler(h)

    # Silence noisy libraries unless debug mode
    if not settings.DEBUG:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a named structlog logger.

    Usage anywhere in the app:
        from app.core.logging import get_logger
        logger = get_logger(__name__)
        logger.info("thing_happened", key="value")

    The __name__ argument gives the logger the module's dotted path
    (e.g. "app.integrations.shopify.service") which appears in every
    log line as the "logger" field. Useful for filtering in log files.
    """
    return structlog.get_logger(name)