"""
Configuration Layer — Environment-Aware Settings
==================================================

HOW IT WORKS:
  The ENVIRONMENT variable is the single switch that changes where secrets come from.

  ┌─────────────────┬──────────────────────────────────────────────────────┐
  │ ENVIRONMENT     │ Where secrets are read from                          │
  ├─────────────────┼──────────────────────────────────────────────────────┤
  │ development     │ .env file on disk (local Docker Compose)             │
  │ test            │ .env.test file, or .env if no .env.test exists       │
  │ production      │ GCP Secret Manager (Cloud Run)                       │
  └─────────────────┴──────────────────────────────────────────────────────┘

  Non-secret config (LOG_LEVEL, APP_NAME, etc.) always comes from environment
  variables — injected by Docker Compose locally, by Cloud Run YAML in production.

  Secrets (JWT_SECRET_KEY, ENCRYPTION_KEY, OAuth secrets) are NEVER stored
  in environment variables in production. They are fetched from GCP Secret
  Manager at startup by secret_manager.py and merged into Settings before
  the app accepts any traffic.

TWO-PHASE LOADING:
  Phase 1 — Pydantic reads all values from env vars / .env file.
             The Settings object is created. In production, SecretStr fields
             will hold placeholder values from env vars (which Cloud Run sets
             to a sentinel like "__FROM_SECRET_MANAGER__").

  Phase 2 — FastAPI lifespan calls build_settings(). If ENVIRONMENT=production,
             load_production_secrets() is called. It fetches real values from
             Secret Manager and updates the Settings object in place.
             This completes before the app begins accepting requests.

  The rest of the codebase only ever calls settings.JWT_SECRET_KEY.get_secret_value()
  and gets the real value. It never needs to know how it was loaded.

WHY SecretStr FOR SENSITIVE FIELDS:
  Pydantic's SecretStr wraps a string so that:
    - str(secret_str)    → "**********"  (safe to log)
    - repr(secret_str)   → "SecretStr('**********')"
    - secret_str.get_secret_value() → "actual_value"  (only in business logic)
  This means secrets never accidentally appear in FastAPI's /docs, error messages,
  structured log output, or debug prints.

ADDING A NEW SECRET:
  1. Add field to Settings as: MY_NEW_SECRET: SecretStr = SecretStr("")
  2. Add to SECRET_MANAGER_MAPPINGS in secret_manager.py:
       "MY_NEW_SECRET": "d2c-my-new-secret"
  3. Create in GCP: gcloud secrets create d2c-my-new-secret --replication-policy=automatic
  4. Grant access: see secret_manager.py grant_service_account_access()
  5. Locally: add MY_NEW_SECRET=value to .env
"""

import os
from functools import lru_cache
from typing import List

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str | None:
    """
    Return the correct .env file path for the current environment.

    Called once at module import time via the model_config default_factory.
    In production (Cloud Run), no file exists — Cloud Run injects env vars
    directly into the container process. Returning None skips file loading.
    """
    env = os.getenv("ENVIRONMENT", "development")

    if env == "production":
        return None                          # Cloud Run: no file, vars from env

    if env == "test":
        # Allow a separate .env.test so test values don't pollute development
        return ".env.test" if os.path.exists(".env.test") else ".env"

    # development — load from .env (docker compose writes this)
    return ".env"


class Settings(BaseSettings):
    """
    All application configuration in one typed, validated object.

    FIELD TYPES:
      str        — non-sensitive. Safe to log, safe as plain env vars in Cloud Run.
      bool/int   — non-sensitive config values.
      SecretStr  — sensitive. Masked in logs and reprs. In production, the value
                   is populated by secret_manager.py after Phase 1 loading.

    HOW TO READ A SecretStr IN CODE:
      settings.JWT_SECRET_KEY.get_secret_value()   ← correct
      settings.JWT_SECRET_KEY                      ← gives SecretStr object (masked)
      str(settings.JWT_SECRET_KEY)                 ← prints "**********" (safe)

    Never use settings.JWT_SECRET_KEY directly in string formatting or comparisons.
    Always call .get_secret_value() at the point of use.
    """

    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",           # silently ignore unknown env vars
        case_sensitive=False,     # DATABASE_URL == database_url
    )

    # ══════════════════════════════════════════════════════════════════════════
    # NON-SECRET CONFIG
    # These are safe as plain env vars in Cloud Run YAML.
    # They identify behaviour, not credentials.
    # ══════════════════════════════════════════════════════════════════════════

    # ── App identity ──────────────────────────────────────────────────────────
    APP_NAME: str = "D2C Platform"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"    # development | test | production
    DEBUG: bool = False

    # ── Database connection ───────────────────────────────────────────────────
    # Local default: plain postgres container from docker-compose.yml
    # Production:    assembled by secret_manager.py using the Cloud SQL socket path
    #                + DB password from Secret Manager
    DATABASE_URL: str = "postgresql+asyncpg://d2c:d2c_secret@localhost:5432/d2c_db"
    DATABASE_SYNC_URL: str = "postgresql+psycopg2://d2c:d2c_secret@localhost:5432/d2c_db"

    # DB_POOL_SIZE=0  → NullPool (correct for Cloud Run + Cloud SQL Proxy)
    # DB_POOL_SIZE=10 → QueuePool (correct for local / long-running server)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ── JWT non-secret config ─────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Integration endpoints ─────────────────────────────────────────────────
    # Points to mock servers in dev/test; real Shopify/Meta URLs in production
    SHOPIFY_MOCK_BASE_URL: str = "http://mock-shopify:8001"
    META_MOCK_BASE_URL: str = "http://mock-meta:8002"
    SHOPIFY_CLIENT_ID: str = "mock_shopify_client_id"
    META_APP_ID: str = "mock_meta_app_id"

    # ── Background task config ────────────────────────────────────────────────
    SYNC_MAX_RETRIES: int = 3
    SYNC_RETRY_BACKOFF_BASE: float = 2.0    # seconds; base for exponential backoff

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"               # "json" = GCP Cloud Logging, "text" = local

    # ── CORS ──────────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    # ── GCP resource identifiers (not credentials) ────────────────────────────
    # Set as plain env vars in Cloud Run YAML; empty string means local/non-GCP
    GCP_PROJECT_ID: str = ""               # e.g. "my-gcp-project"
    GCP_REGION: str = "us-central1"
    CLOUD_SQL_INSTANCE: str = ""           # e.g. "my-project:us-central1:d2c-db"
    # DB username for the Cloud SQL app user (password comes from Secret Manager)
    CLOUD_SQL_DB_USER: str = "d2c_app"
    CLOUD_SQL_DB_NAME: str = "d2c_db"
    LOG_FILE_ENABLED: bool = False
    LOG_FILE_PATH: str = "logs/app.log"
    LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024
    LOG_FILE_BACKUP_COUNT: int = 5
    # ══════════════════════════════════════════════════════════════════════════
    # SECRETS
    # In development: read from .env file by Pydantic (Phase 1)
    # In production:  placeholder values here; real values injected by
    #                 secret_manager.py (Phase 2) before the app starts
    #
    # Default values are intentionally weak/obvious so that:
    #   a) Local dev works without any setup (docker compose up just works)
    #   b) If Phase 2 fails in production, the app refuses to start because
    #      the weak defaults will fail validation (JWT signing fails, Fernet
    #      key length check fails, etc.)
    # ══════════════════════════════════════════════════════════════════════════

    # ── JWT signing key ───────────────────────────────────────────────────────
    # Production secret name: d2c-jwt-secret
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET_KEY: SecretStr = SecretStr("dev_jwt_secret_NOT_FOR_PRODUCTION")
    # ── Fernet token encryption key ───────────────────────────────────────────
    # Production secret name: d2c-encryption-key
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Supports comma-separated keys for rotation: "new_key,old_key"
    ENCRYPTION_KEY: SecretStr = SecretStr("dev_encryption_key_NOT_FOR_PRODUCTION")

    # ── Shopify OAuth secrets ─────────────────────────────────────────────────
    # Production secret names: d2c-shopify-client-secret, d2c-shopify-webhook-secret
    SHOPIFY_CLIENT_SECRET: SecretStr = SecretStr("mock_shopify_client_secret")
    SHOPIFY_WEBHOOK_SECRET: SecretStr = SecretStr("mock_webhook_hmac_secret")

    # ── Meta OAuth secret ─────────────────────────────────────────────────────
    # Production secret name: d2c-meta-app-secret
    META_APP_SECRET: SecretStr = SecretStr("mock_meta_app_secret")

    # ── Database password (production only) ───────────────────────────────────
    # Production secret name: d2c-db-password
    # Used by secret_manager.py to assemble the full DATABASE_URL for Cloud SQL
    DB_PASSWORD: SecretStr = SecretStr("d2c_secret")   # local docker-compose default

    # ══════════════════════════════════════════════════════════════════════════
    # COMPUTED PROPERTIES
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def is_test(self) -> bool:
        return self.ENVIRONMENT == "test"


# ── Module-level singleton ─────────────────────────────────────────────────────
# Phase 1 only. In production, secrets are placeholders until build_settings()
# is called in the FastAPI lifespan (main.py).

@lru_cache
def get_settings() -> Settings:
    """
    Phase 1: Load all non-secret config from env vars / .env file.

    Returns Settings with:
      - All non-secret fields populated from env vars
      - SecretStr fields populated from .env (development) OR
        holding sentinel values (production, until Phase 2 runs)

    lru_cache ensures this runs exactly once per process.
    Call get_settings.cache_clear() in tests to reset between test cases.
    """
    return Settings()


async def build_settings() -> Settings:
    """
    Full two-phase settings initialisation. Called in FastAPI lifespan (main.py).

    Phase 1 already completed by get_settings() at import time.
    Phase 2 (production only): fetch real secrets from GCP Secret Manager
    and update the Settings object before the first request is served.

    Usage in main.py lifespan:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await build_settings()   # Phase 2 secrets loaded here
            yield
    """
    s = get_settings()
    if s.is_production:
        from app.core.secret_manger import load_production_secrets
        await load_production_secrets(s)
    return s


# Convenience module-level import. Most code uses:
#   from app.core.config import settings
settings = get_settings()