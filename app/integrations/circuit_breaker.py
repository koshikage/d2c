"""
Circuit Breaker for Integration HTTP Clients
=============================================
Prevents cascade failures when an upstream API (Shopify, Meta) is down.

THE PROBLEM WITHOUT A CIRCUIT BREAKER:
  Shopify has a 30-minute outage.
  Every sync job hits Shopify, waits for timeout (30s), retries 3 times = 90s blocked.
  With 100 brands, the sync scheduler has 100 × 90s = 150 minutes of blocked workers.
  New sync requests keep queuing. The scheduler falls behind by hours.
  When Shopify recovers, all 100 brands slam it simultaneously → rate limited again.

THE CIRCUIT BREAKER PATTERN:
  CLOSED   → Normal operation. Requests go through.
  OPEN     → Too many failures. Requests fail immediately (no HTTP call made).
  HALF-OPEN→ After reset_timeout, one test request is allowed through.
             If it succeeds: back to CLOSED.
             If it fails: back to OPEN.

  This gives Shopify/Meta time to recover without our code hammering them.
  When OPEN, sync jobs fail fast with a clear error (not after 90s timeout).

PER-PROVIDER STATE:
  The circuit breaker is keyed on (provider, brand_id).
  Brand A's Shopify 401s don't affect Brand B's syncs.
  A global Shopify outage (5xx from all brands) opens the breaker globally.

  In production: circuit state should be stored in Redis (shared across
  Cloud Run instances). Here we use in-process dict (single-instance only).
  Phase 2: replace _state with a Redis-backed store.

THRESHOLDS (tunable via env):
  failure_threshold: 5 failures → open the circuit
  reset_timeout: 60 seconds before trying again
  half_open_max_calls: 1 test call allowed in half-open state
"""
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from app.core.logging import get_logger

logger = get_logger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerState:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""
    def __init__(self, provider: str, retry_after: float):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker OPEN for {provider}. "
            f"Retry after {retry_after:.0f}s."
        )


# In-process state. Replace with Redis in multi-instance deployments.
_circuits: dict[str, CircuitBreakerState] = defaultdict(CircuitBreakerState)
_lock = asyncio.Lock()

FAILURE_THRESHOLD = 5
RESET_TIMEOUT_SECONDS = 60.0
HALF_OPEN_MAX_CALLS = 1


def _circuit_key(provider: str, scope: str = "global") -> str:
    return f"{provider}:{scope}"


async def call_with_circuit_breaker(
    provider: str,
    scope: str,
    coro: Coroutine[Any, Any, Any],
) -> Any:
    """
    Execute a coroutine through the circuit breaker for (provider, scope).

    provider: "shopify" | "meta"
    scope: "global" or brand_id (for per-brand circuits)
    coro: the async HTTP call to make

    Usage:
        result = await call_with_circuit_breaker(
            "shopify", str(brand_id),
            client.get(urls.products, params=params)
        )
    """
    key = _circuit_key(provider, scope)

    async with _lock:
        cb = _circuits[key]
        now = time.monotonic()

        if cb.state == CircuitState.OPEN:
            elapsed = now - cb.last_failure_time
            if elapsed < RESET_TIMEOUT_SECONDS:
                raise CircuitBreakerOpen(
                    provider=f"{provider}/{scope}",
                    retry_after=RESET_TIMEOUT_SECONDS - elapsed,
                )
            # Timeout elapsed: transition to HALF_OPEN
            cb.state = CircuitState.HALF_OPEN
            cb.half_open_calls = 0
            logger.info("circuit_half_open", provider=provider, scope=scope)

        if cb.state == CircuitState.HALF_OPEN:
            if cb.half_open_calls >= HALF_OPEN_MAX_CALLS:
                raise CircuitBreakerOpen(
                    provider=f"{provider}/{scope}",
                    retry_after=5.0,
                )
            cb.half_open_calls += 1

    # Execute the call outside the lock
    try:
        result = await coro
        # Success: reset the circuit
        async with _lock:
            cb = _circuits[key]
            if cb.state in (CircuitState.HALF_OPEN, CircuitState.CLOSED):
                if cb.failure_count > 0:
                    logger.info(
                        "circuit_recovered",
                        provider=provider,
                        scope=scope,
                        previous_failures=cb.failure_count,
                    )
                cb.state = CircuitState.CLOSED
                cb.failure_count = 0
        return result

    except Exception as exc:
        async with _lock:
            cb = _circuits[key]
            cb.failure_count += 1
            cb.last_failure_time = time.monotonic()

            if cb.state == CircuitState.HALF_OPEN:
                # Failed test call: go back to OPEN
                cb.state = CircuitState.OPEN
                logger.warning(
                    "circuit_reopened",
                    provider=provider,
                    scope=scope,
                    error=str(exc),
                )
            elif cb.failure_count >= FAILURE_THRESHOLD:
                cb.state = CircuitState.OPEN
                logger.error(
                    "circuit_opened",
                    provider=provider,
                    scope=scope,
                    failure_count=cb.failure_count,
                    error=str(exc),
                )
        raise
