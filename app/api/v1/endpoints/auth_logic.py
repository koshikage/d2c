"""
Auth Endpoint — Route Logic
=============================
Handles the mock Google OAuth login flow and current-user introspection.

LOGIN FLOW:
  1. Client sends { email, brand_domain }
  2. We look up the User by email
  3. We verify user.brand.domain matches brand_domain (prevents cross-brand login)
  4. We issue a JWT carrying user_id + brand_id
  5. Client stores the JWT and sends it as Authorization: Bearer <token>

WHY VERIFY brand_domain IN LOGIN:
  If a user from Brand A somehow knows the email of a Brand B user,
  they shouldn't be able to log into Brand B. The brand_domain check
  ties the login to the correct tenant.

PROS:
  + Tenant-scoped login: prevents cross-brand impersonation
  + Simple: no session store, no refresh tokens (Phase 1)
  + last_login_at updated on every login for audit trail

CONS:
  - No rate limiting on login attempts (add slowapi in production)
  - Mock: no real Google token verification
  - No account lockout after failed attempts
"""
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints.auth_serializers import LoginRequest, TokenResponse, UserResponse
from app.api.v1.endpoints.auth_urls import router
from app.core.jwt import create_access_token
from app.core.logging import get_logger
from app.db.session import get_db
from app.middleware.auth import CurrentUser, get_current_user
from app.models import Brand, User

logger = get_logger(__name__)


@router.post("/login", response_model=TokenResponse, summary="Mock OAuth Login")
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Mock Google OAuth login endpoint.

    Accepts an email + brand_domain, validates the user exists and belongs
    to that brand, then issues a signed JWT.

    In production: accept a Google ID token, verify it with Google's API,
    extract the email, then proceed from step 2.
    """
    # Load user with their brand in one query (avoids N+1)
    result = await db.execute(
        select(User)
        .where(User.email == body.email, User.is_active == True)  # noqa: E712
        .options(selectinload(User.brand))
    )
    user = result.scalars().first()

    if not user:
        # Return the same error for "user not found" and "wrong brand"
        # to avoid user enumeration attacks
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Verify brand domain matches — prevents Brand A user logging into Brand B
    if user.brand.domain != body.brand_domain:
        logger.warning(
            "login_wrong_brand",
            email=body.email,
            attempted_domain=body.brand_domain,
            actual_domain=user.brand.domain,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if not user.brand.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand account is suspended",
        )

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    await db.commit()

    token = create_access_token(
        user_id=str(user.id),
        brand_id=str(user.brand_id),
        email=user.email,
    )

    from app.core.config import settings
    logger.info("login_success", user_id=str(user.id), brand_id=str(user.brand_id))

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse, summary="Current User Info")
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the authenticated user's profile."""
    result = await db.execute(
        select(User)
        .where(User.id == current_user.id)
        .options(selectinload(User.brand))
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        brand_id=user.brand_id,
        brand_name=user.brand.name,
        created_at=user.created_at,
    )
