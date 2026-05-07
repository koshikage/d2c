"""
Auth Endpoint — Serializers
=============================
Request and response Pydantic schemas for the auth endpoints.

DESIGN DECISIONS:
  - LoginRequest accepts email only (mock OAuth — no real Google token needed).
    A real implementation would accept a Google ID token and verify it via
    Google's tokeninfo endpoint or a local public key.

  - TokenResponse returns only the access token and its type.
    We deliberately do NOT return refresh tokens in Phase 1 (YAGNI).
    A refresh token flow would be: separate endpoint + httpOnly cookie.

  - UserResponse exposes only safe fields — no internal UUIDs to the client
    unless needed. Here we do expose brand_id since the frontend needs it
    for display purposes.

PROS:
  + Pydantic validates incoming email is a real email format
  + Separate schemas for request vs response prevents over-posting attacks
  + model_config from_attributes=True allows direct ORM → schema conversion

CONS:
  - Email-only login is not production-ready; real Google OAuth adds complexity
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """
    Mock Google OAuth login.
    In production: replace with Google ID token verification.
    """
    email: EmailStr
    brand_domain: str = Field(
        ...,
        description="The brand domain to log into. User must belong to this brand.",
        examples=["brand-alpha.myshopify.com"],
    )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    brand_id: uuid.UUID
    brand_name: str
    created_at: datetime

    model_config = {"from_attributes": True}
