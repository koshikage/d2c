"""
Shopify Integration Endpoints — Serializers
=============================================
Request/response schemas for all Shopify integration API endpoints.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ShopifyConnectRequest(BaseModel):
    """Body for POST /integrations/shopify/connect"""
    shop_domain: str = Field(
        ...,
        description="The .myshopify.com domain of the store",
        examples=["brand-alpha.myshopify.com"],
    )


class ShopifyConnectResponse(BaseModel):
    """
    Returned after initiating the OAuth flow.
    The frontend should redirect the user to authorize_url.
    """
    authorize_url: str
    state: str
    message: str = "Redirect the user to authorize_url to complete Shopify connection"


class ShopifyCallbackRequest(BaseModel):
    """Query params from Shopify's OAuth callback."""
    code: str
    state: str
    shop: str


class ShopifyConnectionStatus(BaseModel):
    """Current connection state for the authenticated brand."""
    is_connected: bool
    shop_domain: str | None
    last_synced_at: datetime | None
    token_expires_at: datetime | None

    model_config = {"from_attributes": True}


class SyncTriggerResponse(BaseModel):
    """Response after triggering a manual sync."""
    message: str
    products_synced: int
    orders_synced: int
    brand_id: uuid.UUID


class WebhookAckResponse(BaseModel):
    """Acknowledge webhook receipt."""
    received: bool
    event_id: str
    message: str
