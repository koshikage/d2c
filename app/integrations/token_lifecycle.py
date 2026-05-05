"""
Integration Token Lifecycle
=============================
Manages OAuth token expiry, refresh, and proactive rotation.

THE PROBLEM WITH JUST STORING TOKENS:
  An access token stored at OAuth connect time will eventually expire.
  For Shopify: tokens don't expire (unless the merchant uninstalls the app).
  For Meta: user tokens expire in 60 days; system user tokens in 60 days.
  
  If we don't track expiry and attempt a sync with an expired token:
  1. The sync fails with a 401
  2. We log an error
  3. The merchant's data goes stale
  4. Someone has to manually reconnect

TOKEN LIFECYCLE STATES:
  VALID        → token_expires_at is None (Shopify) or > now + buffer
  EXPIRING     → token_expires_at is within the refresh_buffer window
  EXPIRED      → token_expires_at < now
  MISSING      → access_token_enc is None (never connected or revoked)

STRATEGY:
  1. Check token state before every sync
  2. If EXPIRING: attempt proactive refresh (for providers that support it)
  3. If EXPIRED: mark connection as disconnected, emit alert
  4. If MISSING: raise IntegrationNotConnectedError (don't sync)

  For Shopify (tokens don't expire): state is always VALID once connected.
  For Meta (60-day expiry): attempt token refresh using the long-lived token
  flow 5 days before expiry.

WHY 5-DAY BUFFER (not 1 day):
  Sync jobs run periodically. If the buffer is 1 day and the job happens
  to not run for 25 hours, the token expires before refresh. 5 days gives
  5 missed sync cycles to recover.
"""
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

from app.core.encryption import decrypt_token, encrypt_token
from app.core.logging import get_logger

logger = get_logger(__name__)

REFRESH_BUFFER_DAYS = 5  # Refresh if expiry is within 5 days


class TokenState(str, Enum):
    VALID = "valid"
    EXPIRING = "expiring"      # within buffer, refresh now
    EXPIRED = "expired"        # past expiry, must reconnect
    MISSING = "missing"        # never connected or revoked


class IntegrationNotConnectedError(Exception):
    """Raised when a sync is attempted for an unconnected or expired integration."""
    pass


def get_token_state(
    access_token_enc: str | None,
    token_expires_at: datetime | None,
) -> TokenState:
    """
    Determine the current lifecycle state of an OAuth token.

    Shopify tokens have no expiry (token_expires_at = None) → always VALID.
    Meta tokens expire in 60 days.
    """
    if not access_token_enc:
        return TokenState.MISSING

    if token_expires_at is None:
        # Provider doesn't expire tokens (Shopify)
        return TokenState.VALID

    now = datetime.now(tz=timezone.utc)

    if token_expires_at < now:
        return TokenState.EXPIRED

    if token_expires_at < now + timedelta(days=REFRESH_BUFFER_DAYS):
        return TokenState.EXPIRING

    return TokenState.VALID


def get_decrypted_token_or_raise(
    access_token_enc: str | None,
    token_expires_at: datetime | None,
    brand_id: str,
    provider: str,
) -> str:
    """
    Validate token lifecycle state and decrypt.
    Raises IntegrationNotConnectedError if not usable.

    The EXPIRING state is logged as a warning but doesn't block the sync —
    we still proceed with the current token while scheduling a refresh.
    The caller should handle the warning by triggering a background refresh.
    """
    state = get_token_state(access_token_enc, token_expires_at)

    if state == TokenState.MISSING:
        raise IntegrationNotConnectedError(
            f"{provider} integration for brand {brand_id} has no access token. "
            "Brand must reconnect via OAuth."
        )

    if state == TokenState.EXPIRED:
        raise IntegrationNotConnectedError(
            f"{provider} token for brand {brand_id} has expired "
            f"(expired at {token_expires_at}). Brand must reconnect via OAuth."
        )

    if state == TokenState.EXPIRING:
        logger.warning(
            "token_expiring_soon",
            provider=provider,
            brand_id=brand_id,
            expires_at=str(token_expires_at),
            days_remaining=str(
                (token_expires_at - datetime.now(tz=timezone.utc)).days
                if token_expires_at else "N/A"
            ),
        )
        # Continue with current token but signal that refresh is needed.
        # In Phase 2: enqueue a background refresh task here.

    return decrypt_token(access_token_enc)  # type: ignore[arg-type]
