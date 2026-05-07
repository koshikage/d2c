"""
STEP 10 — Mock Shopify Server
================================
A small FastAPI app that simulates the real Shopify Admin REST API.

WHY A REAL MOCK SERVER (not just fixtures):
  - Tests the full HTTP client path: retries, pagination cursor parsing, headers
  - Can simulate 429 rate limits, 5xx errors, slow responses
  - Provides realistic Shopify response shapes including Link headers for pagination

HOW SHOPIFY PAGINATION WORKS (replicated here):
  Shopify uses cursor-based pagination via the Link header:
    Link: <https://shop.myshopify.com/admin/api/products.json?page_info=CURSOR>; rel="next"
  Our sync client parses this header to get the next page URL and fetches it directly.

  IMPORTANT — mock vs production URL in the Link header:
    Real Shopify: Link header contains the full shop domain URL
                  e.g. https://brand.myshopify.com/admin/api/...
    This mock:    Link header must contain the mock server's own base URL
                  e.g. http://mock-shopify:8001/admin/api/...
    If the mock returned the real shop domain in the Link header, the sync
    client would try to hit the real Shopify API on page 2 and fail.
    We use the request's own base URL (from the Host header) so the Link
    always points back to this mock server regardless of where it is running.

RATE LIMIT SIMULATION:
  Shopify uses a leaky-bucket model: 40 calls/second normally, 2/second in burst.
  We simulate 429 responses to test our retry/backoff logic.
"""
import random
import string
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

mock_shopify = FastAPI(title="Mock Shopify API", version="1.0.0")

# ── Fake data store ───────────────────────────────────────────────────────────

def _make_products(brand_prefix: str, count: int = 25) -> list[dict]:
    products = []
    for i in range(1, count + 1):
        products.append({
            "id": f"gid://shopify/Product/{brand_prefix}{i:04d}",
            "title": f"Product {brand_prefix}-{i}",
            "vendor": f"Vendor {brand_prefix}",
            "product_type": random.choice(["Apparel", "Accessories", "Electronics"]),
            "status": "active",
            "variants": [
                {
                    "id": f"gid://shopify/ProductVariant/{brand_prefix}{i:04d}01",
                    "price": str(round(random.uniform(9.99, 199.99), 2)),
                    "inventory_quantity": random.randint(0, 500),
                }
            ],
            "created_at": (datetime.now(timezone.utc) - timedelta(days=random.randint(1, 365))).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    return products


def _make_orders(brand_prefix: str, count: int = 30) -> list[dict]:
    orders = []
    for i in range(1, count + 1):
        amount = round(random.uniform(20.0, 500.0), 2)
        orders.append({
            "id": f"gid://shopify/Order/{brand_prefix}{i:04d}",
            "order_number": f"#{1000 + i}",
            "total_price": str(amount),
            "subtotal_price": str(round(amount * 0.9, 2)),
            "currency": "USD",
            "financial_status": random.choice(["paid", "pending", "refunded"]),
            "fulfillment_status": random.choice(["fulfilled", "unfulfilled", None]),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=random.randint(0, 90))).isoformat(),
        })
    return orders


# Pre-generate products and orders for two mock brands
MOCK_DATA = {
    "brand_a": {
        "products": _make_products("A", 25),
        "orders": _make_orders("A", 30),
    },
    "brand_b": {
        "products": _make_products("B", 20),
        "orders": _make_orders("B", 20),
    },
}

PAGE_SIZE = 10
CALL_COUNT = 0
REQUEST_COUNTS: dict[str, list[float]] = {}


def _get_page(items: list, page_info: str | None) -> tuple[list, str | None]:
    """
    Cursor-based pagination. The cursor is just the integer start offset,
    encoded as a string (opaque to the client — mirrors how Shopify works).
    """
    start = 0
    if page_info:
        try:
            start = int(page_info)
        except ValueError:
            start = 0

    page = items[start: start + PAGE_SIZE]
    next_cursor = str(start + PAGE_SIZE) if (start + PAGE_SIZE) < len(items) else None
    return page, next_cursor


def _get_brand_data(shop: str) -> dict:
    """Map a shop domain to mock data."""
    if "brand-b" in shop or "brandb" in shop:
        return MOCK_DATA["brand_b"]
    return MOCK_DATA["brand_a"]


def _mock_base_url(request: Request) -> str:
    """
    Return the base URL of THIS mock server from the incoming request.

    WHY: The Link header must point back to the mock server, not to
    a real Shopify domain. We derive the base URL from the request
    so this works whether the mock is at localhost:8001 or mock-shopify:8001
    (Docker Compose) or any other host.

    Result: "http://mock-shopify:8001"  or  "http://localhost:8001"
    """
    return str(request.base_url).rstrip("/")


@mock_shopify.get("/admin/api/2024-01/products.json")
async def list_products(
    request: Request,
    shop: str = Query(default="brand-a.myshopify.com"),
    page_info: str | None = Query(default=None),
    limit: int = Query(default=10),
):
    """
    Paginated products endpoint with Link header pagination.

    KEY FIX — Link header uses the mock server's own base URL:
      Before: https://{shop}/admin/api/products.json?page_info={cursor}
              → sync client follows this to the real Shopify API on page 2+
      After:  http://mock-shopify:8001/admin/api/products.json?page_info={cursor}
              → sync client follows this back to this mock server ✓

    The URL is derived from the incoming request so it works on any host.
    """
    global CALL_COUNT
    CALL_COUNT += 1

    # Simulate occasional 429 (every 12th request) to exercise retry logic
    if CALL_COUNT % 12 == 0:
        return JSONResponse(
            status_code=429,
            content={"errors": "Exceeded 2 calls per second for api client"},
            headers={"Retry-After": "2.0", "X-Shopify-Shop-Api-Call-Limit": "40/40"},
        )

    brand_data = _get_brand_data(shop)
    items, next_cursor = _get_page(brand_data["products"], page_info)

    response_headers = {}
    if next_cursor:
        # Build the Link header using THIS server's base URL — not the shop domain.
        # This is what makes multi-page pagination work in local/Docker environments.
        base = _mock_base_url(request)
        response_headers["Link"] = (
            f'<{base}/admin/api/2024-01/products.json'
            f'?shop={shop}&page_info={next_cursor}>; rel="next"'
        )

    return JSONResponse(content={"products": items}, headers=response_headers)


@mock_shopify.get("/admin/api/2024-01/orders.json")
async def list_orders(
    request: Request,
    shop: str = Query(default="brand-a.myshopify.com"),
    page_info: str | None = Query(default=None),
    status: str = Query(default="any"),
):
    """Paginated orders endpoint. Same Link header fix as list_products."""
    brand_data = _get_brand_data(shop)
    items, next_cursor = _get_page(brand_data["orders"], page_info)

    response_headers = {}
    if next_cursor:
        base = _mock_base_url(request)
        response_headers["Link"] = (
            f'<{base}/admin/api/2024-01/orders.json'
            f'?shop={shop}&status={status}&page_info={next_cursor}>; rel="next"'
        )

    return JSONResponse(content={"orders": items}, headers=response_headers)


@mock_shopify.post("/admin/api/2024-01/webhooks.json")
async def register_webhook(request: Request):
    """Simulate webhook registration."""
    body = await request.json()
    webhook_id = "".join(random.choices(string.digits, k=10))
    return JSONResponse(
        content={
            "webhook": {
                "id": webhook_id,
                "address": body.get("webhook", {}).get("address", ""),
                "topic": body.get("webhook", {}).get("topic", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        status_code=201,
    )