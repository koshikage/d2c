"""
API v1 Router
==============
Assembles all endpoint routers into the v1 API.

WHY VERSION THE API (/api/v1/...):
  - Allows breaking changes without disrupting existing clients
  - v2 can coexist with v1 during migration
  - Industry standard for REST APIs

PATTERN:
  Each feature module contributes:
    - *_urls.py    → APIRouter with prefix/tags
    - *_logic.py   → route handler functions registered on that router
  We import the router from *_urls.py (which *_logic.py also imports and decorates).
  This file just mounts them all under /api/v1.
"""
from fastapi import APIRouter

# Import logic modules to trigger route registration on their routers
import app.api.v1.endpoints.auth_logic  # noqa: F401
import app.api.v1.endpoints.insights_logic  # noqa: F401
import app.api.v1.endpoints.meta_logic  # noqa: F401
import app.api.v1.endpoints.shopify_logic  # noqa: F401

from app.api.v1.endpoints.auth_urls import router as auth_router
from app.api.v1.endpoints.insights_urls import router as insights_router
from app.api.v1.endpoints.meta_urls import router as meta_router
from app.api.v1.endpoints.shopify_urls import router as shopify_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth_router)
api_router.include_router(shopify_router)
api_router.include_router(meta_router)
api_router.include_router(insights_router)
