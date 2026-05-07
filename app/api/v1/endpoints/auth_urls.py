"""
Auth Endpoint — URLs
======================
/auth/login   POST  — mock Google OAuth: accepts email, returns JWT
/auth/me      GET   — returns current user info (protected)
"""
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["Authentication"])
