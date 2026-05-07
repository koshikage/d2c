"""
STEP 12 — Resilient HTTP Client (Integration Base)
====================================================
All external API calls go through this client, which handles:
  - Exponential backoff on 429 / 5xx
  - Retry-After header respect
  - Idempotent request logging
  - Per-brand rate limit context in logs

WHY EXPONENTIAL BACKOFF (not linear):
  If a server is overloaded and 100 clients all retry after exactly 2 seconds,
  they all hit at once → "thundering herd" → server stays overloaded.
  Exponential backoff with jitter spreads retries across time.

BACKOFF FORMULA:
  wait = base * (2 ** attempt) + random_jitter(0, 1)
  attempt 0 → ~2s, attempt 1 → ~4s, attempt 2 → ~8s

WHY httpx (not aiohttp):
  - requests-compatible API
  - First-class async support with AsyncClient
  - Better timeout control (connect + read timeouts separately)

PROS:
  + Resilient against transient failures and rate limits
  + All retry logic centralised — integration modules don't duplicate it
  + Per-brand context in every log line

CONS:
  - Too-aggressive retries can DDoS a real API if backoff config is wrong
  - No circuit breaker (Phase 2 improvement: use tenacity or pybreaker)
"""
import asyncio
import random
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class IntegrationHTTPClient:
    """
    Async HTTP client wrapper with automatic retry + exponential backoff.
    Instantiate once per sync job; close after use.

    Usage:
        async with IntegrationHTTPClient() as client:
            data = await client.get("https://api.example.com/resource")
    """

    def __init__(
        self,
        base_url: str = "",
        default_headers: dict[str, str] | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
    ):
        self._base_url = base_url
        self._default_headers = default_headers or {}
        self._max_retries = max_retries or settings.SYNC_MAX_RETRIES
        self._backoff_base = backoff_base or settings.SYNC_RETRY_BACKOFF_BASE
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "IntegrationHTTPClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._default_headers,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def get(self, url: str, **kwargs: Any) -> dict:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> dict:
        return await self._request("POST", url, **kwargs)

    async def get_with_headers(
        self, url: str, **kwargs: Any
    ) -> tuple[dict, "httpx.Headers"]:
        """
        Same as get() but also returns the response headers.

        Used when callers need to read response headers alongside the body —
        specifically for Shopify's Link header pagination, where the next-page
        URL is in the header, not the body.

        Returns:
            (json_body, response_headers)

        Usage:
            data, headers = await client.get_with_headers(url, params=params)
            next_url = _parse_next_link(headers.get("Link"))
        """
        assert self._client is not None, "Client not initialised — use as async context manager"

        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request("GET", url, **kwargs)

                if response.status_code == 429:
                    wait = self._parse_retry_after(response) or self._backoff(attempt)
                    logger.warning(
                        "rate_limited",
                        url=url,
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code in RETRYABLE_STATUS_CODES:
                    wait = self._backoff(attempt)
                    logger.warning(
                        "retryable_error",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()

                logger.debug("http_success", method="GET", url=url, status=response.status_code)
                return response.json(), response.headers

            except httpx.TransportError as e:
                last_exception = e
                if attempt < self._max_retries:
                    wait = self._backoff(attempt)
                    logger.warning("transport_error", url=url, error=str(e), wait_seconds=wait)
                    await asyncio.sleep(wait)
                    continue
                raise

        raise RuntimeError(
            f"Request to {url} failed after {self._max_retries} retries. Last error: {last_exception}"
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict:
        """
        Execute an HTTP request with retry + exponential backoff.

        Retry conditions:
          - 429 Too Many Requests: respect Retry-After header or backoff
          - 5xx Server Error: backoff and retry
          - httpx.TransportError (network hiccup): retry immediately once

        Non-retryable:
          - 4xx (except 429): bug in our request, don't retry
        """
        assert self._client is not None, "Client not initialised — use as async context manager"

        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)

                if response.status_code == 429:
                    wait = self._parse_retry_after(response) or self._backoff(attempt)
                    logger.warning(
                        "rate_limited",
                        url=url,
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code in RETRYABLE_STATUS_CODES:
                    wait = self._backoff(attempt)
                    logger.warning(
                        "retryable_error",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()

                logger.debug("http_success", method=method, url=url, status=response.status_code)
                return response.json()

            except httpx.TransportError as e:
                # Network-level error (DNS, connection reset). Retry once.
                last_exception = e
                if attempt < self._max_retries:
                    wait = self._backoff(attempt)
                    logger.warning("transport_error", url=url, error=str(e), wait_seconds=wait)
                    await asyncio.sleep(wait)
                    continue
                raise

        raise RuntimeError(
            f"Request to {url} failed after {self._max_retries} retries. Last error: {last_exception}"
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter to avoid thundering herd."""
        cap = 60.0
        base = self._backoff_base * (2 ** attempt)
        return random.uniform(0, min(cap, base))

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse the Retry-After header if present. Returns seconds to wait."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return None