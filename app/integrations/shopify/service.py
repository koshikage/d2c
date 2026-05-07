"""
Shopify Integration — Sync Logic (Service Layer)
=================================================
Orchestrates the actual data pull from Shopify and persistence to our DB.

RESPONSIBILITIES:
  1. OAuth connect: initiate, exchange code for token, store encrypted
  2. Background sync: paginate products + orders, upsert idempotently
  3. Webhook ingestion: verify HMAC, persist event, process idempotently
  4. Pagination: follow Link headers until exhausted

IDEMPOTENCY STRATEGY:
  All upserts use INSERT ... ON CONFLICT (brand_id, external_id) DO UPDATE
  This means running the sync twice produces the same result — no duplicates.
  This is critical for:
    - Retry safety (if sync crashes mid-way and restarts)
    - Webhook replay (Shopify guarantees at-least-once delivery)

  WHY NOT check-then-insert:
    Race condition: two concurrent sync jobs could both see "not exists"
    and both attempt to insert → unique constraint violation.
    ON CONFLICT is atomic at the DB level.

HMAC WEBHOOK VERIFICATION:
  Shopify signs webhook payloads with HMAC-SHA256 using your webhook secret.
  Header: X-Shopify-Hmac-SHA256: <base64(hmac(secret, body))>
  We compute the same HMAC and compare with constant-time comparison
  to prevent timing attacks.

PROS:
  + Idempotent sync: safe to retry at any point
  + HMAC verification prevents fake webhook injection
  + Incremental sync via last_synced_at: only pull new data after first full sync

CONS:
  - ON CONFLICT upserts update ALL fields — if Shopify sends stale data
    in a retry, it could overwrite newer data (mitigate: add updated_at comparison)
  - No parallelism across pages (sequential pagination). Phase 2: parallel page fetch.
"""
import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import encrypt_token
from app.core.logging import get_logger
from app.integrations.base_client import IntegrationHTTPClient
from app.integrations.circuit_breaker import CircuitBreakerOpen, call_with_circuit_breaker
from app.integrations.shopify.serializers import (
    ShopifyOrderIn,
    ShopifyOrdersPage,
    ShopifyProductIn,
    ShopifyProductsPage,
    ShopifyWebhookOrderPayload,
)
from app.integrations.shopify.urls import ShopifyURLs
from app.integrations.token_lifecycle import IntegrationNotConnectedError, get_decrypted_token_or_raise
from app.models import Order, Product, ShopifyConnection, WebhookEvent

logger = get_logger(__name__)


# ── OAuth Flow ────────────────────────────────────────────────────────────────

async def initiate_shopify_oauth(
    db: AsyncSession,
    brand_id: uuid.UUID,
    shop_domain: str,
) -> dict:
    """
    STEP 1 of Shopify OAuth.
    Generate a random state token (CSRF protection), store it, return the
    redirect URL that the frontend should send the user to.

    In a real flow, the user's browser follows this URL to Shopify,
    grants permission, and Shopify redirects back to our callback URL.
    """
    import secrets
    state = secrets.token_urlsafe(32)

    # Upsert connection record with pending state
    stmt = pg_insert(ShopifyConnection).values(
        id=uuid.uuid4(),
        brand_id=brand_id,
        shop_domain=shop_domain,
        oauth_state=state,
        is_connected=False,
    ).on_conflict_do_update(
        index_elements=["brand_id"],
        set_={"shop_domain": shop_domain, "oauth_state": state, "is_connected": False},
    )
    await db.execute(stmt)
    await db.commit()

    authorize_url = ShopifyURLs.oauth_authorize(
        shop_domain=shop_domain,
        client_id=settings.SHOPIFY_CLIENT_ID,
        state=state,
        redirect_uri="https://your-app.com/integrations/shopify/callback",
    )

    logger.info("shopify_oauth_initiated", brand_id=str(brand_id), shop=shop_domain)
    return {"authorize_url": authorize_url, "state": state}


async def complete_shopify_oauth(
    db: AsyncSession,
    brand_id: uuid.UUID,
    shop_domain: str,
    code: str,
    state: str,
) -> ShopifyConnection:
    """
    STEP 2: Exchange the authorization code for an access token.
    Verify the state matches (CSRF check).
    Store the token encrypted at rest.
    """
    # Load existing connection and verify state
    from sqlalchemy import select
    result = await db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.brand_id == brand_id,
            ShopifyConnection.shop_domain == shop_domain,
        )
    )
    connection = result.scalars().first()

    if not connection:
        raise ValueError(f"No pending Shopify connection for brand {brand_id}")

    if connection.oauth_state != state:
        logger.warning("shopify_oauth_state_mismatch", brand_id=str(brand_id))
        raise PermissionError("OAuth state mismatch — possible CSRF attack")

    # Exchange code for token (mocked: real Shopify posts to oauth/access_token)
    # In real flow this would be an HTTP POST to ShopifyURLs.oauth_token(shop_domain)
    mock_token = f"shpat_mock_{brand_id}_{shop_domain}"

    connection.access_token_enc = encrypt_token(mock_token)
    connection.is_connected = True
    connection.oauth_state = None  # consume the state — prevent replay
    db.add(connection)
    await db.commit()
    await db.refresh(connection)

    logger.info("shopify_oauth_completed", brand_id=str(brand_id), shop=shop_domain)
    return connection


# ── Pagination Helper ────────────────────────────────────────────────────────

def _parse_next_link(link_header: str | None) -> str | None:
    """
    Parse Shopify's Link header to extract the next page URL.
    Format: <https://shop.myshopify.com/admin/api/products.json?page_info=CURSOR>; rel="next"
    """
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


# ── Product Sync ─────────────────────────────────────────────────────────────

PAGE_SIZE = 10  # items per page, matches mock server PAGE_SIZE


async def sync_products(
    db: AsyncSession,
    brand_id: uuid.UUID,
    connection: ShopifyConnection,
) -> int:
    """
    Pull all products from Shopify (paginated) and upsert into our DB.
    Returns total number of products upserted.

    PAGINATION LOOP:
      1. GET urls.products with shop + limit params → first page
      2. Parse Link header for rel="next" URL
      3. GET that URL as-is (it already contains shop + page_info from the server)
      4. Repeat until no Link header is returned

    WHY get_with_headers() and not get():
      Shopify's pagination cursor lives in the response Link header, not the body.
      get_with_headers() returns (body, headers) through the same retry/backoff
      logic as get() — no private _client access needed.
    """
    token = get_decrypted_token_or_raise(
        connection.access_token_enc,
        connection.token_expires_at,
        brand_id=str(brand_id),
        provider="shopify",
    )
    urls = ShopifyURLs(settings.SHOPIFY_MOCK_BASE_URL)

    total_upserted = 0
    # First request: explicit params. From page 2 onwards the next_url
    # already carries all params (shop, page_info) — params must be empty.
    next_url: str | None = urls.products
    params: dict[str, str | int] = {"shop": connection.shop_domain, "limit": PAGE_SIZE}

    async with IntegrationHTTPClient() as client:
        while next_url:
            logger.info(
                "shopify_products_page_fetch",
                brand_id=str(brand_id),
                url=next_url,
            )

            data, resp_headers = await client.get_with_headers(
                next_url,
                params=params or None,
                headers={"X-Shopify-Access-Token": token},
            )

            products = [ShopifyProductIn(**p) for p in data.get("products", [])]
            count = await _upsert_products(db, brand_id, products)
            total_upserted += count

            logger.debug(
                "shopify_products_page_done",
                brand_id=str(brand_id),
                page_count=len(products),
                total_so_far=total_upserted,
            )

            # Advance to next page. next_url is None when no Link header is present.
            next_url = _parse_next_link(resp_headers.get("Link"))
            params = {}  # page 2+: all params are already in next_url

    connection.last_synced_at = datetime.now(timezone.utc)
    db.add(connection)
    await db.commit()

    logger.info("shopify_products_sync_complete", brand_id=str(brand_id), total=total_upserted)
    return total_upserted




async def _upsert_products(
    db: AsyncSession,
    brand_id: uuid.UUID,
    products: list[ShopifyProductIn],
) -> int:
    """
    Bulk upsert products using PostgreSQL INSERT ... ON CONFLICT DO UPDATE.

    WHY BULK UPSERT:
      - One DB round-trip per page (not one per product)
      - Atomic per-page: either all products in the page are written or none
      - ON CONFLICT guarantees idempotency at DB level
    """
    if not products:
        return 0

    rows = [
        {
            "id": uuid.uuid4(),
            "brand_id": brand_id,
            "external_id": p.id,
            "title": p.title,
            "vendor": p.vendor,
            "product_type": p.product_type,
            "price": p.primary_price,
            "inventory_quantity": p.total_inventory,
            "status": p.status,
            "raw_data": p.raw,
        }
        for p in products
    ]

    stmt = pg_insert(Product).values(rows).on_conflict_do_update(
        constraint="uq_product_brand_external",
        set_={
            "title": pg_insert(Product).excluded.title,
            "vendor": pg_insert(Product).excluded.vendor,
            "product_type": pg_insert(Product).excluded.product_type,
            "price": pg_insert(Product).excluded.price,
            "inventory_quantity": pg_insert(Product).excluded.inventory_quantity,
            "status": pg_insert(Product).excluded.status,
            "raw_data": pg_insert(Product).excluded.raw_data,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(rows)


# ── Order Sync ───────────────────────────────────────────────────────────────

async def sync_orders(
    db: AsyncSession,
    brand_id: uuid.UUID,
    connection: ShopifyConnection,
) -> int:
    """Paginate and upsert all orders. Same pattern as sync_products."""
    token = get_decrypted_token_or_raise(
        connection.access_token_enc,
        connection.token_expires_at,
        brand_id=str(brand_id),
        provider="shopify",
    )
    urls = ShopifyURLs(settings.SHOPIFY_MOCK_BASE_URL)

    total_upserted = 0
    next_url: str | None = urls.orders
    params: dict[str, str | int] = {"shop": connection.shop_domain, "status": "any", "limit": PAGE_SIZE}

    async with IntegrationHTTPClient() as client:
        while next_url:
            logger.info("shopify_orders_page_fetch", brand_id=str(brand_id), url=next_url)

            data, resp_headers = await client.get_with_headers(
                next_url,
                params=params or None,
                headers={"X-Shopify-Access-Token": token},
            )

            orders = [ShopifyOrderIn(**o) for o in data.get("orders", [])]
            count = await _upsert_orders(db, brand_id, orders)
            total_upserted += count

            next_url = _parse_next_link(resp_headers.get("Link"))
            params = {}

    connection.last_synced_at = datetime.now(timezone.utc)
    db.add(connection)
    await db.commit()

    logger.info("shopify_orders_sync_complete", brand_id=str(brand_id), total=total_upserted)
    return total_upserted


async def _upsert_orders(
    db: AsyncSession,
    brand_id: uuid.UUID,
    orders: list[ShopifyOrderIn],
) -> int:
    if not orders:
        return 0

    rows = [
        {
            "id": uuid.uuid4(),
            "brand_id": brand_id,
            "external_id": o.id,
            "order_number": o.order_number,
            "total_price": o.total_price,
            "subtotal_price": o.subtotal_price,
            "currency": o.currency,
            "financial_status": o.financial_status,
            "fulfillment_status": o.fulfillment_status,
            "ordered_at": o.created_at,
            "webhook_received": False,
            "raw_data": o.raw,
        }
        for o in orders
    ]

    stmt = pg_insert(Order).values(rows).on_conflict_do_update(
        constraint="uq_order_brand_external",
        set_={
            "order_number": pg_insert(Order).excluded.order_number,
            "total_price": pg_insert(Order).excluded.total_price,
            "subtotal_price": pg_insert(Order).excluded.subtotal_price,
            "financial_status": pg_insert(Order).excluded.financial_status,
            "fulfillment_status": pg_insert(Order).excluded.fulfillment_status,
            "raw_data": pg_insert(Order).excluded.raw_data,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(rows)


# ── Webhook Ingestion ────────────────────────────────────────────────────────

def verify_shopify_hmac(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify a Shopify webhook HMAC-SHA256 signature.

    Shopify computes: HMAC-SHA256(webhook_secret, raw_request_body)
    and sends it base64-encoded in X-Shopify-Hmac-SHA256.

    We compute the same and compare with hmac.compare_digest (constant-time)
    to prevent timing side-channel attacks.

    IMPORTANT: Use raw bytes body — never re-serialised JSON.
    Parsing and re-serialising changes whitespace/key order and breaks the HMAC.

    SecretStr fix: SHOPIFY_WEBHOOK_SECRET is a SecretStr. Calling .encode()
    on it directly raises AttributeError. Always call .get_secret_value() first.
    """
    secret_bytes = settings.SHOPIFY_WEBHOOK_SECRET.get_secret_value().encode()
    expected = base64.b64encode(
        hmac.new(secret_bytes, raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature_header)


async def process_order_webhook(
    db: AsyncSession,
    brand_id: uuid.UUID,
    raw_body: bytes,
    payload: dict,
    external_id: str,
) -> WebhookEvent:
    """
    Persist a webhook event and process it idempotently.

    PERSIST-FIRST PATTERN:
      1. Store the raw event (so we can replay it if processing fails)
      2. Mark as processed=False initially
      3. Attempt to upsert the order
      4. Mark as processed=True

    This means even a crash between steps 3 and 4 can be recovered
    by re-running unprocessed events.

    IDEMPOTENCY:
      We check if a WebhookEvent with the same external_id already exists
      for this brand. If it does AND processed=True, we skip. This handles
      Shopify's at-least-once webhook delivery (same event sent twice).
    """
    from sqlalchemy import select

    # Check for duplicate webhook (Shopify sends same event ≥1 times)
    existing = await db.execute(
        select(WebhookEvent).where(
            WebhookEvent.brand_id == brand_id,
            WebhookEvent.external_id == external_id,
            WebhookEvent.event_type == "orders/created",
        )
    )
    existing_event = existing.scalars().first()

    if existing_event and existing_event.processed:
        logger.info("webhook_duplicate_skipped", external_id=external_id, brand_id=str(brand_id))
        return existing_event

    # Persist raw event
    event = WebhookEvent(
        brand_id=brand_id,
        provider="shopify",
        event_type="orders/created",
        external_id=external_id,
        payload=payload,
        processed=False,
    )
    db.add(event)
    await db.flush()

    try:
        # Parse and upsert the order
        order_data = ShopifyWebhookOrderPayload(**payload)
        await _upsert_orders(db, brand_id, [
            ShopifyOrderIn(
                id=order_data.id,
                order_number=order_data.order_number,
                total_price=order_data.total_price,
                subtotal_price=order_data.subtotal_price,
                currency=order_data.currency,
                financial_status=order_data.financial_status,
                fulfillment_status=order_data.fulfillment_status,
                created_at=order_data.created_at,
            )
        ])
        event.processed = True
        event.processed_at = datetime.now(timezone.utc)
        logger.info("webhook_processed", external_id=external_id, brand_id=str(brand_id))
    except Exception as e:
        event.processing_error = str(e)
        logger.error("webhook_processing_failed", external_id=external_id, error=str(e))

    db.add(event)
    await db.commit()
    return event