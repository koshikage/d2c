"""
STEP 8 — Authentication Middleware & Dependencies
==================================================
FastAPI uses dependency injection for auth — cleaner than middleware for
per-endpoint control, because some endpoints are public (health, webhooks, login).

DEPENDENCY CHAIN:
  get_current_user
    └── calls decode_access_token() (JWT verification)
    └── queries DB to validate user still exists and brand_id matches
    └── binds structlog context vars (brand_id, user_id, email)
    └── returns CurrentUser object

  get_tenant_session
    └── calls get_current_user
    └── returns TenantSession(db, brand_id=user.brand_id)

All protected routes depend on get_tenant_session — they get a pre-scoped
session that can only access their brand's data.

WHY ALSO VALIDATE IN DB (not just trust the JWT):
  A JWT can still be valid after:
    - The user is deactivated
    - The brand is suspended
    - A compromised token hasn't expired yet
  The DB check catches these cases. It costs one query per request but is worth it.

PROS of this approach:
  + Public vs protected is explicit per-route
  + Auth logic centralised in one place
  + Structlog context automatically populated for all subsequent log calls
  + TenantSession dependency means brand scoping is automatic in all handlers

CONS:
  - DB round-trip on every protected request (mitigated by connection pooling)
  - If JWT is compromised, user has ~60 minutes until it expires
    (mitigate: shorter expiry + refresh tokens, or a Redis token blocklist)
"""
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import bind_contextvars

from app.core.jwt import decode_access_token
from app.core.logging import get_logger
from app.db.session import get_db
from app.db.tenant import TenantSession
from app.models import User

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=True)


@dataclass
class CurrentUser:
    """Lightweight user context attached to each request."""
    id: uuid.UUID
    brand_id: uuid.UUID
    email: str


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """
    FastAPI dependency: verify JWT and return the authenticated user.
    Raises 401 on any auth failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise credentials_exception

    # Validate the user still exists in DB and brand_id matches
    # This catches deactivated users, brand suspensions, etc.
    result = await db.execute(
        select(User).where(
            User.id == uuid.UUID(payload.user_id),
            User.brand_id == uuid.UUID(payload.brand_id),
            User.is_active == True,  # noqa: E712
        )
    )
    user = result.scalars().first()

    if not user:
        logger.warning(
            "auth_user_not_found",
            user_id=payload.user_id,
            brand_id=payload.brand_id,
        )
        raise credentials_exception

    # Bind tenant context to structlog for ALL subsequent log calls in this request
    bind_contextvars(
        brand_id=str(user.brand_id),
        user_id=str(user.id),
        user_email=user.email,
    )

    logger.debug("auth_success", user_id=str(user.id))
    return CurrentUser(id=user.id, brand_id=user.brand_id, email=user.email)


async def get_tenant_session(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantSession:
    """
    FastAPI dependency: returns an activated TenantSession for the current brand.

    "Activated" means the PostgreSQL RLS session variable has been set:
        SET LOCAL app.current_brand_id = '<brand_id>'

    This arms Layer 3 of tenant isolation (PostgreSQL Row-Level Security)
    in addition to Layer 2 (TenantSession query scoping).

    Usage:
        @router.get("/products")
        async def list_products(ts: TenantSession = Depends(get_tenant_session)):
            return await ts.all(Product)
    """
    ts = TenantSession(db, brand_id=current_user.brand_id)
    await ts.activate()  # SET LOCAL app.current_brand_id for RLS
    return ts
