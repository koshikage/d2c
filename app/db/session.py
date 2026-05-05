"""
Database Session — Async SQLAlchemy Engine
==========================================
Two pooling modes depending on deployment context:

LOCAL / DOCKER-COMPOSE:
  Standard QueuePool (pool_size=10, max_overflow=20).
  Connections reused across requests — efficient for a long-running process.

CLOUD RUN (DB_POOL_SIZE=0 → NullPool):
  Each request opens and closes its own connection via the Cloud SQL Proxy.
  Counterintuitive but correct for Cloud Run because:
    1. Cloud Run scales to zero: pooled connections are wasted when idle
       and exhaust Cloud SQL's connection limit across many sleeping instances.
    2. The Cloud SQL Auth Proxy handles pooling at the socket layer.
    3. Documented GCP recommendation for Cloud Run + Cloud SQL.
  Set DB_POOL_SIZE=0 in infra/cloudrun-service.yaml to activate.
"""
from collections.abc import AsyncGenerator

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_use_null_pool = settings.DB_POOL_SIZE == 0

if _use_null_pool:
    async_engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        echo=settings.DEBUG,
    )
else:
    async_engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=settings.DEBUG,
    )

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Per-request database session. Commits on success, rolls back on error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("db_health_check_failed", error=str(e))
        return False
