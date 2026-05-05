"""
Shopify Integration Endpoints — Route Logic
=============================================
Handles OAuth connect, sync trigger, webhook ingestion.

WEBHOOK SECURITY:
  The webhook endpoint is PUBLIC (no JWT auth) because Shopify calls it,
  not our users. Instead we verify the HMAC signature using the raw request body.
  We read raw bytes BEFORE Pydantic parses the body — parsing changes whitespace
  and would invalidate the HMAC.

BACKGROUND SYNC:
  For Phase 1 we run sync synchronously in the request (simple, observable).
  Phase 2: move to a Celery/Cloud Tasks background job so the HTTP response
  returns immediately and sync runs asynchronously.

  PROS of sync-in-request (Phase 1):
    + Simple: no task queue infrastructure
    + Easy to debug: logs and errors are in the request trace
  CONS:
    - Blocks the request for the sync duration (30+ seconds for large stores)
    - Timeout risk on long syncs
"""
import json

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.shopify_serializers import (
    ShopifyCallbackRequest,
    ShopifyConnectRequest,
    ShopifyConnectResponse,
    ShopifyConnectionStatus,
    SyncTriggerResponse,
    WebhookAckResponse,
)
from app.api.v1.endpoints.shopify_urls import router
from app.core.logging import get_logger
from app.db.session import get_db
from app.integrations.shopify.service import (
    complete_shopify_oauth,
    initiate_shopify_oauth,
    process_order_webhook,
    sync_orders,
    sync_products,
    verify_shopify_hmac,
)
from app.middleware.auth import CurrentUser, get_current_user
from app.models import ShopifyConnection

logger = get_logger(__name__)


@router.post("/connect", response_model=ShopifyConnectResponse)
async def connect_shopify(
    body: ShopifyConnectRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Initiate Shopify OAuth flow for the authenticated brand.
    Returns the Shopify authorize URL for the frontend to redirect to.
    """
    result = await initiate_shopify_oauth(
        db=db,
        brand_id=current_user.brand_id,
        shop_domain=body.shop_domain,
    )
    return ShopifyConnectResponse(authorize_url=result["authorize_url"], state=result["state"])


@router.get("/callback")
async def shopify_oauth_callback(
    code: str,
    state: str,
    shop: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Shopify's OAuth callback.
    In production: Shopify redirects here after the merchant approves.
    For the mock: call this directly with the state from /connect.
    """
    connection = await complete_shopify_oauth(
        db=db,
        brand_id=current_user.brand_id,
        shop_domain=shop,
        code=code,
        state=state,
    )
    return {"message": "Shopify connected successfully", "shop": connection.shop_domain}


@router.post("/sync", response_model=SyncTriggerResponse)
async def trigger_sync(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger an immediate sync of products and orders for the authenticated brand.
    Runs synchronously (blocks until complete). See module docstring for Phase 2 notes.
    """
    result = await db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.brand_id == current_user.brand_id,
            ShopifyConnection.is_connected == True,  # noqa: E712
        )
    )
    connection = result.scalars().first()

    if not connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active Shopify connection. Please connect first.",
        )

    products_count = await sync_products(db, current_user.brand_id, connection)
    orders_count = await sync_orders(db, current_user.brand_id, connection)

    return SyncTriggerResponse(
        message="Sync completed successfully",
        products_synced=products_count,
        orders_synced=orders_count,
        brand_id=current_user.brand_id,
    )


@router.post("/webhook", response_model=WebhookAckResponse)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default="orders/created"),
    x_shopify_shop_domain: str | None = Header(default=None),
):
    """
    Receive Shopify order-created webhooks.

    AUTHENTICATION: HMAC signature verification (not JWT — Shopify calls this, not users).
    IDEMPOTENCY: Same order_id received twice → second is silently deduped.

    NOTE: We resolve brand_id from the shop domain stored in our DB.
    """
    # Must read raw bytes BEFORE any JSON parsing to preserve HMAC integrity
    raw_body = await request.body()

    # Verify HMAC
    if not x_shopify_hmac_sha256:
        raise HTTPException(status_code=401, detail="Missing HMAC signature")

    if not verify_shopify_hmac(raw_body, x_shopify_hmac_sha256):
        logger.warning("webhook_hmac_invalid", shop=x_shopify_shop_domain)
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # Parse payload
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Resolve brand from shop domain
    if not x_shopify_shop_domain:
        raise HTTPException(status_code=400, detail="Missing X-Shopify-Shop-Domain header")

    result = await db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.shop_domain == x_shopify_shop_domain,
            ShopifyConnection.is_connected == True,  # noqa: E712
        )
    )
    connection = result.scalars().first()

    if not connection:
        logger.warning("webhook_unknown_shop", shop=x_shopify_shop_domain)
        # Return 200 to prevent Shopify from retrying — we don't know this shop
        return WebhookAckResponse(
            received=True,
            event_id="unknown",
            message="Shop not registered",
        )

    external_id = str(payload.get("id", ""))
    event = await process_order_webhook(
        db=db,
        brand_id=connection.brand_id,
        raw_body=raw_body,
        payload=payload,
        external_id=external_id,
    )

    return WebhookAckResponse(
        received=True,
        event_id=str(event.id),
        message="Webhook processed" if event.processed else "Webhook received, processing deferred",
    )


@router.get("/status", response_model=ShopifyConnectionStatus)
async def connection_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the Shopify connection status for the authenticated brand."""
    result = await db.execute(
        select(ShopifyConnection).where(ShopifyConnection.brand_id == current_user.brand_id)
    )
    connection = result.scalars().first()

    if not connection:
        return ShopifyConnectionStatus(
            is_connected=False,
            shop_domain=None,
            last_synced_at=None,
            token_expires_at=None,
        )

    return ShopifyConnectionStatus(
        is_connected=connection.is_connected,
        shop_domain=connection.shop_domain,
        last_synced_at=connection.last_synced_at,
        token_expires_at=connection.token_expires_at,
    )
